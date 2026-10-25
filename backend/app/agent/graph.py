"""
graph.py — Wires all 11 nodes into a LangGraph StateGraph.

Built nodes (1, 2, 3+4, 5, 6, 7, 8, 9) call your real implementations.
Nodes not yet built (10, 11) are stub implementations marked clearly below —
replace their bodies once those files are written; the graph wiring itself
won't need to change since the state shape is already defined for them.

Run flow:
  classify -> [preflight refusal] -> return_save -> END
           -> grade_schema -> [exhausted] -> escalation -> END
                                                      -> [ok] -> business_rules
                                                                 -> generate_query -> [unanswerable] -> return_save -> END
                                                                                    -> [error] -> escalation -> END
                                                                                    -> [ok] -> safety_check -> [violation] -> escalation -> END
                                                                                                              -> [ok] -> execute -> [exhausted] -> escalation -> END
                                                                                                                                  -> [ok] -> result_grader -> [invalid] -> escalation -> END
                                                                                                                                                            -> [valid] -> return_save -> END
"""

import copy
import logging
import re
import re
import uuid
from typing import Any
from app.agent.nodes.classifier_rewrite import (
    ClassificationError,
    _enforce_allowed_clarification_topic,
    classify_and_rewrite,
)

from langgraph.graph import StateGraph, END

from app.agent.state import GraphState, new_state
from app.agent.nodes.return_save import build_answer
from app.agent.nodes.escalation import escalate
from app.agent.nodes.grade_sample import (
    grade_schema_sufficiency,
    GradeSampleError,
    MissingRelationshipError,
    SchemaGraderUnavailableError,
    schema_diagnostics_scope,
)
from app.agent.nodes.business_rules import find_rules_by_terms
from app.agent.nodes.schema_grounding import get_cached_schema
from app.agent.nodes.generate_query import (
    generate_query,
    repair_query_after_safety_rejection,
    QueryGenerationError,
    UnanswerableQuestionError,
    preflight_refusals,
    query_diagnostics_scope,
)
from app.agent.catalog_resolver import validate_catalog_filter_plan
from app.agent.plan_contract import ranking_policy_for
from app.agent.nodes.safety_check import (
    REPAIRABLE_SAFETY_RULES,
    run_safety_check,
    SafetyViolationError,
)
from app.agent.nodes.run_self_fix import (
    QueryExecutionError,
    QueryInfrastructureError,
    QueryRepairError,
    run_query,
    run_query_with_repair,
)
from app.agent.nodes.did_you_mean import (
    build_suggestion_execution_plan,
    find_value_suggestions,
)
from app.agent.nodes.result_grader import grade_result, ResultGraderError
from app.agent.prompts.grader_prompt import (
    build_ranked_result_note,
    ranked_result_metadata,
)
from app.services.timing import timed_node, start_request
from app.services.errors import infrastructure_failure_code
from app.services.langfuse_tracing import create_callback_handler
from app.services.suggestion_store import store_suggestion_plans
from app.services.conversation_context import (
    get_conversation_context,
    store_verified_turn,
)

logger = logging.getLogger(__name__)
_FOLLOWUP_REFERENCE_PATTERN = re.compile(
    r"\b(?:that|those|these|them|it|same)\b|'\s*s\s+total\b",
    re.IGNORECASE,
)


def _record_infrastructure_failure(
    state: GraphState, error: BaseException, stage: str
) -> None:
    diagnostics = _ensure_diagnostics(state)
    code = infrastructure_failure_code(error)
    if code is None:
        return
    failures = diagnostics["infrastructure_errors"]
    if not any(item.get("code") == code and item.get("stage") == stage for item in failures):
        failures.append({
            "code": code,
            "stage": stage,
            "detail": str(error),
        })


def _ensure_diagnostics(state: GraphState) -> dict[str, Any]:
    diagnostics = state.get("diagnostics")
    if isinstance(diagnostics, dict):
        diagnostics.setdefault("execution_repairs", [])
        return diagnostics
    diagnostics = {
        "schema_attempts": [],
        "matched_business_rules": [],
        "query_plan": None,
        "generated_sql": None,
        "validation_attempts": [],
        "safety_results": [],
        "execution_repairs": [],
        "refusal": None,
        "infrastructure_errors": [],
    }
    diagnostics.setdefault("execution_repairs", [])
    state["diagnostics"] = diagnostics
    return diagnostics


# ---------------------------------------------------------------------------
# Node 1: Classifier + Rewrite
# ---------------------------------------------------------------------------

async def node_classify(state: GraphState) -> GraphState:
    _ensure_diagnostics(state)
    try:
        classifier_options: dict[str, Any] = {
            "asked_items": state.get("asked_items", []),
        }
        selected_filters = state.get("selected_catalog_filters", [])
        if selected_filters:
            classifier_options["selected_catalog_filters"] = selected_filters
        result = await classify_and_rewrite(
            state["user_question"],
            state.get("conversation_context", []),
            **classifier_options,
        )
        result = _enforce_allowed_clarification_topic({
            **result,
            "original_question": result.get(
                "original_question", state["user_question"]
            ),
        })
        state["classifier_result"] = result
    except ClassificationError as e:
        logger.error("Node 1 failed: %s", e)
        _record_infrastructure_failure(state, e, "classifier_rewrite")
        state["diagnostics"]["refusal"] = {
            "code": "CLASSIFIER_FAILURE",
            "stage": "classifier_rewrite",
            "detail": str(e),
        }
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "classifier_rewrite",
            "reason": str(e),
            "attempts_made": 1,
        }
        return state
    if state["classifier_result"].get("intent") == "data":
        try:
            preflight_refusals(
                state["classifier_result"]["rewritten_question"],
                state["classifier_result"]["original_question"],
            )
        except UnanswerableQuestionError as e:
            state["answerable"] = False
            state["unanswerable_reasoning"] = e.reasoning
            state["diagnostics"]["refusal"] = {
                "code": "PREFLIGHT_UNANSWERABLE",
                "stage": "classifier_rewrite",
                "detail": e.reasoning,
            }
    elif state["classifier_result"].get("intent") == "clarification":
        state["clarification_needed"] = True
        state["clarification_question"] = result["clarification_question"]
        state["clarification_options"] = result["clarification_options"]
        state["diagnostics"]["refusal"] = {
            "code": "CLARIFICATION_REQUIRED",
            "stage": "classifier_rewrite",
        }
    return state


def _flatten_filter_expression(expression: Any) -> list[dict]:
    if not isinstance(expression, dict):
        return []
    if expression.get("boolean") in {"AND", "OR"}:
        return [
            condition
            for child in expression.get("conditions", [])
            for condition in _flatten_filter_expression(child)
        ]
    return [expression]


def _inherit_verified_followup_filters(
    generated_query: dict, prior_turn: dict
) -> dict:
    """Carry prior metric filters into an explicit drill-down query."""
    inherited_expression = prior_turn.get("filters")
    inherited = _flatten_filter_expression(inherited_expression)
    if not inherited:
        return generated_query

    query_tables = {generated_query.get("table")}
    query_tables.update(
        join.get("table")
        for join in generated_query.get("joins", [])
        if isinstance(join, dict)
    )
    missing_tables = {
        condition.get("table")
        for condition in inherited
        if isinstance(condition, dict)
        and condition.get("table") not in query_tables
    }
    if missing_tables:
        raise QueryGenerationError(
            "The follow-up query did not include the table containing its "
            f"verified prior filters: {sorted(missing_tables)}."
        )

    existing = generated_query.get("where_expression")
    existing_conditions = _flatten_filter_expression(existing)
    inherited_is_and_group = (
        isinstance(inherited_expression, dict)
        and inherited_expression.get("boolean") == "AND"
    )
    if (
        inherited_expression in existing_conditions
        or existing == inherited_expression
        or (
            inherited_is_and_group
            and all(condition in existing_conditions for condition in inherited)
        )
    ):
        return generated_query

    updated = dict(generated_query)
    updated["where_expression"] = (
        inherited_expression
        if existing is None
        else {
            "boolean": "AND",
            "conditions": [existing, inherited_expression],
        }
    )
    return updated


# ---------------------------------------------------------------------------
# Node 4 (wraps Node 3 internally via grade_schema_sufficiency's own retry loop)
# ---------------------------------------------------------------------------

async def node_grade_schema(state: GraphState) -> GraphState:
    _ensure_diagnostics(state)
    question = state["classifier_result"]["rewritten_question"]
    try:
        with schema_diagnostics_scope(state["diagnostics"]):
            result = await grade_schema_sufficiency(
                question, state["classifier_result"].get("original_question")
            )
        state["grade_sample_result"] = result
        state["schema_context"] = result["schema"]
        state["schema_relationships"] = result["relationships"]
        state["grade_sample_attempts"] = result["attempts"]
        state["grade_sample_exhausted"] = False
    except MissingRelationshipError as e:
        logger.info("Node 4: %s; routing to a refusal.", e)
        state["diagnostics"]["refusal"] = {
            "code": e.reason_code,
            "stage": "grade_schema",
            "tables": list(e.tables),
            "detail": e.detail,
        }
        state["grade_sample_exhausted"] = False
        state["answerable"] = False
        state["unanswerable_reasoning"] = e.reasoning
    except SchemaGraderUnavailableError as e:
        logger.error("Node 4 grader unavailable: %s", e)
        _record_infrastructure_failure(state, e, "grade_schema")
        state["grade_sample_exhausted"] = True
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "grade_sample",
            "reason": f"SERVICE_UNAVAILABLE: {e}",
            "attempts_made": 1,
        }
    except GradeSampleError as e:
        logger.error("Node 4 exhausted retries: %s", e)
        state["diagnostics"]["refusal"] = {
            "code": "SCHEMA_GRADING_EXHAUSTED",
            "stage": "grade_schema",
            "detail": str(e),
        }
        state["grade_sample_exhausted"] = True
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "grade_sample",
            "reason": str(e),
            "attempts_made": state.get("retry_caps", {}).get("grade_sample", 3),
        }
    return state


# ---------------------------------------------------------------------------
# Node 5: Business Rules
# ---------------------------------------------------------------------------

async def node_business_rules(state: GraphState) -> GraphState:
    _ensure_diagnostics(state)
    rule_terms = state["classifier_result"].get(
        "matched_business_rule_terms", []
    )
    state["matched_business_rules"] = find_rules_by_terms(rule_terms)
    state["diagnostics"]["matched_business_rules"] = state[
        "matched_business_rules"
    ]
    return state


# ---------------------------------------------------------------------------
# Node 6: Generate Query
# ---------------------------------------------------------------------------

async def node_generate_query(state: GraphState) -> GraphState:
    _ensure_diagnostics(state)
    question = state["classifier_result"]["rewritten_question"]
    schema_context = state["schema_context"]
    matched_rules = state["matched_business_rules"]

    try:
        generation_options = {"request_id": state.get("request_id")}
        catalog_filters = state["classifier_result"].get("catalog_filters", [])
        generation_options["resolved_temporal_constraints"] = (
            state["classifier_result"].get("resolved_temporal_constraints", [])
        )
        generation_options["current_state_filters"] = (
            state["classifier_result"].get("current_state_filters", [])
        )
        if catalog_filters:
            generation_options["catalog_filters"] = catalog_filters
            generation_options["catalog_filter_combiner"] = state[
                "classifier_result"
            ].get("catalog_filter_combiner")
        with query_diagnostics_scope(state["diagnostics"]):
            result = await generate_query(
                question,
                schema_context,
                matched_rules,
                state.get("schema_relationships", []),
                state["classifier_result"]["original_question"],
                **generation_options,
            )
        original_question = state["classifier_result"].get("original_question", "")
        prior_context = state.get("conversation_context", [])
        if _FOLLOWUP_REFERENCE_PATTERN.search(original_question) and prior_context:
            result = _inherit_verified_followup_filters(result, prior_context[-1])
        catalog_filters = state["classifier_result"].get("catalog_filters", [])
        if catalog_filters:
            try:
                validate_catalog_filter_plan(result, catalog_filters)
            except ValueError as e:
                raise QueryGenerationError(str(e)) from e
        state["generated_query"] = result
        state["diagnostics"]["query_plan"] = {
            key: value for key, value in result.items() if key != "reasoning"
        }
        state["answerable"] = True
        state["unanswerable_reasoning"] = None
    except UnanswerableQuestionError as e:
        state["answerable"] = False
        state["unanswerable_reasoning"] = e.reasoning
        state["diagnostics"]["refusal"] = {
            "code": "QUERY_UNANSWERABLE",
            "stage": "generate_query",
            "detail": e.reasoning,
        }
    except QueryGenerationError as e:
        logger.error("Node 6 failed: %s", e)
        _record_infrastructure_failure(state, e, "generate_query")
        state["diagnostics"]["refusal"] = {
            "code": "QUERY_GENERATION_FAILED",
            "stage": "generate_query",
            "detail": str(e),
        }
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "generate_query",
            "reason": str(e),
            "attempts_made": 1,
        }
    return state


# ---------------------------------------------------------------------------
# Node 7: Safety Check
# ---------------------------------------------------------------------------

async def node_safety_check(state: GraphState) -> GraphState:
    _ensure_diagnostics(state)
    query = state["generated_query"]
    schema_context = state["schema_context"]
    valid_tables = list(schema_context.keys())
    relationships = state.get("schema_relationships", [])
    state["safety_repair_attempts"] = 0

    try:
        result = run_safety_check(
            query,
            valid_tables=valid_tables,
            schema_context=schema_context,
            relationships=relationships,
        )
        state["diagnostics"]["safety_results"].append({
            "passed": True,
            "rule_code": None,
        })
    except SafetyViolationError as e:
        state["safety_violation_reason"] = str(e)
        state["diagnostics"]["safety_results"].append({
            "passed": False,
            "rule_code": e.rule_id,
            "detail": str(e),
        })
        state["diagnostics"]["refusal"] = {
            "code": f"SAFETY_{e.rule_id or 'VIOLATION'}",
            "stage": "safety_check",
        }
        if e.rule_id not in REPAIRABLE_SAFETY_RULES:
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "safety_check",
                "reason": str(e),
                "attempts_made": 1,
            }
            return state

        max_repairs = 2
        classifier_result = state.get("classifier_result", {})
        original_question = classifier_result.get(
            "original_question", state.get("user_question", "")
        )
        rewritten_question = classifier_result.get(
            "rewritten_question", original_question
        )
        repair_options = {
            "catalog_filters": classifier_result.get("catalog_filters", []),
            "catalog_filter_combiner": classifier_result.get(
                "catalog_filter_combiner"
            ),
            "resolved_temporal_constraints": classifier_result.get(
                "resolved_temporal_constraints", []
            ),
            "current_state_filters": classifier_result.get(
                "current_state_filters", []
            ),
        }
        current_rejected_query = query
        last_error: Exception = e
        result = None
        for repair_attempt in range(1, max_repairs + 1):
            state["safety_repair_attempts"] = repair_attempt
            try:
                repaired_query = await repair_query_after_safety_rejection(
                    current_rejected_query,
                    str(last_error),
                    rewritten_question,
                    schema_context,
                    state.get("matched_business_rules", []),
                    relationships,
                    original_question,
                    **repair_options,
                )
            except UnanswerableQuestionError as repair_error:
                state["unanswerable_reasoning"] = repair_error.reasoning
                reason = repair_error.internal_reasoning or str(repair_error)
                logger.warning(
                    "Safety-plan repair returned verified missing-schema evidence: %s",
                    reason,
                )
                state["escalated"] = True
                state["escalation_info"] = {
                    "node": "safety_check",
                    "reason": reason,
                    "attempts_made": 1 + repair_attempt,
                    "repair_attempts": repair_attempt,
                }
                return state
            except QueryGenerationError as repair_error:
                last_error = repair_error
                state["diagnostics"]["safety_results"].append({
                    "passed": False,
                    "rule_code": getattr(repair_error, "rule_id", None),
                    "detail": str(repair_error),
                    "repair_attempt": repair_attempt,
                })
                code = infrastructure_failure_code(
                    repair_error.__cause__ or repair_error
                )
                if code:
                    _record_infrastructure_failure(
                        state, repair_error, "safety_repair"
                    )
                    break
                continue

            state["generated_query"] = repaired_query
            state["diagnostics"]["query_plan"] = {
                key: value for key, value in repaired_query.items()
                if key != "reasoning"
            }
            try:
                result = run_safety_check(
                    repaired_query,
                    valid_tables=valid_tables,
                    schema_context=schema_context,
                    relationships=relationships,
                )
            except SafetyViolationError as retry_error:
                last_error = retry_error
                current_rejected_query = repaired_query
                state["safety_violation_reason"] = str(retry_error)
                state["diagnostics"]["safety_results"].append({
                    "passed": False,
                    "rule_code": retry_error.rule_id,
                    "detail": str(retry_error),
                    "repair_attempt": repair_attempt,
                })
                continue
            state["diagnostics"]["safety_results"].append({
                "passed": True,
                "rule_code": None,
                "repair_attempt": repair_attempt,
            })
            break

        if result is None:
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "safety_check",
                "reason": f"Safety repair exhausted: {last_error}",
                "attempts_made": 1 + state["safety_repair_attempts"],
                "repair_attempts": state["safety_repair_attempts"],
            }
            return state

    state["safety_check_result"] = result
    validated_query = result.get("validated_query")
    if isinstance(validated_query, dict):
        state["generated_query"] = validated_query
        state["diagnostics"]["query_plan"] = {
            key: value for key, value in validated_query.items()
            if key != "reasoning"
        }
    state["diagnostics"]["generated_sql"] = result.get("sql")
    state["diagnostics"]["safety_results"][-1]["safe_sql"] = result.get("sql")
    state["safety_violation_reason"] = None
    state["escalated"] = False
    return state


# ---------------------------------------------------------------------------
# Node 8: Run + Self-Fix
# run_query_with_repair separates bounded transient-infrastructure retries
# from model-driven repair of recognized SQL-shape errors.
# ---------------------------------------------------------------------------

def _pick_tie_column(generated_query: dict, columns: list) -> str | None:
    """The first ORDER BY entry that is a selected result column. Only an
    aggregate/alias column is used, because a tie on a plain column is not a
    'top N' ambiguity we can detect from the result rows."""
    ranking_intent = generated_query.get("ranking_intent", "none")
    if not ranking_policy_for(ranking_intent)["returns_all_optimal_rows"]:
        return None
    aliases = {
        item.get("alias")
        for item in generated_query.get("select_items", [])
        if isinstance(item, dict) and item.get("alias")
    }
    aliases.update(
        item.get("alias")
        for aggregate in generated_query.get("isolated_child_aggregates", [])
        if isinstance(aggregate, dict)
        for item in aggregate.get("select_items", [])
        if isinstance(item, dict) and item.get("alias")
    )
    aliases.update(
        item.get("alias")
        for item in generated_query.get("derived_metrics", [])
        if isinstance(item, dict) and item.get("alias")
    )
    for order in generated_query.get("order_by", []):
        if not isinstance(order, dict):
            continue
        column = order.get("column")
        if order.get("table") is None and column in aliases and column in columns:
            return column
    return None

def _identifier_column(column: str) -> bool:
    parts = set(value for value in column.casefold().replace("-", "_").split("_") if value)
    identifier_parts = {
        "code", "guid", "identifier", "id", "ids", "mobile", "phone", "pincode",
        "postcode", "uuid", "zipcode",
    }
    return bool(parts & identifier_parts)


def _column_formats(
    generated_query: dict, schema_context: dict, default_table: str
) -> dict[str, str]:
    """Mark identifier-like result columns so clients do not group their digits."""
    formats: dict[str, str] = {}
    for item in generated_query.get("select_items", []):
        if not isinstance(item, dict) or item.get("aggregate"):
            continue

        source_column = item.get("column")
        source_table = item.get("table") or default_table
        if source_column == "*":
            for column_info in schema_context.get(source_table, []):
                if not isinstance(column_info, dict):
                    continue
                name = column_info.get("column")
                if isinstance(name, str) and _identifier_column(name):
                    formats[name] = "identifier"
            continue
        if not isinstance(source_column, str):
            continue

        output_column = item.get("alias") or source_column
        if _identifier_column(source_column) or (
            isinstance(output_column, str) and _identifier_column(output_column)
        ):
            formats[output_column] = "identifier"
    return formats


def _build_validated_execution_query(
    generated_query: dict,
    safety_result: dict,
    schema_context: dict,
    valid_tables: list[str],
    relationships: list[dict],
) -> tuple[dict, list[str]]:
    generated_query = safety_result.get("validated_query", generated_query)
    table = safety_result["table"]
    columns = []
    for item in generated_query.get("select_items", []):
        column = item.get("column")
        aggregate = item.get("aggregate")
        alias = item.get("alias")

        if column == "*" and aggregate is None:
            columns.extend(
                col["column"]
                for col in schema_context.get(table, [])
                if isinstance(col, dict) and "column" in col
            )
        elif alias:
            columns.append(alias)
        elif aggregate:
            columns.append(f"{aggregate.lower()}_{'all' if column == '*' else column}")
        else:
            columns.append(column)
    columns.extend(
        item["alias"]
        for aggregate in generated_query.get("isolated_child_aggregates", [])
        if isinstance(aggregate, dict)
        for item in aggregate.get("select_items", [])
        if isinstance(item, dict)
        and isinstance(item.get("alias"), str)
        and item["alias"] not in columns
    )
    columns.extend(
        item["alias"]
        for item in generated_query.get("derived_metrics", [])
        if isinstance(item, dict)
        and isinstance(item.get("alias"), str)
        and item["alias"] not in columns
    )

    validated_query = {
        "sql": safety_result["sql"],
        "params": tuple(safety_result["params"]),
        "columns": columns,
        "row_limit": safety_result["row_limit"],
        "user_limit": safety_result["user_limit"],
        "count_sql": safety_result.get("count_sql"),
        "count_params": tuple(safety_result.get("count_params", [])),
        "tie_column": _pick_tie_column(generated_query, columns),
        "single_best_with_ties": ranking_policy_for(
            generated_query.get("ranking_intent", "none")
        )["returns_all_optimal_rows"],
        "column_formats": _column_formats(generated_query, schema_context, table),
    }
    return validated_query, columns


def _and_terms(expression: Any) -> list[dict[str, Any]]:
    if not isinstance(expression, dict):
        return []
    if expression.get("boolean") == "AND":
        terms = []
        for child in expression.get("conditions", []):
            terms.extend(_and_terms(child))
        return terms
    return [expression]


def _combine_and_terms(terms: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not terms:
        return None
    if len(terms) == 1:
        return terms[0]
    return {"boolean": "AND", "conditions": terms}


def _filter_diagnostic_label(expression: dict[str, Any]) -> str:
    if expression.get("boolean") in {"AND", "OR"}:
        return f"{expression['boolean']} filter group"
    table = expression.get("table") or expression.get("metric") or "filter"
    column = expression.get("column") or expression.get("metric_alias")
    operator = expression.get("operator", "condition")
    if column is None:
        return str(table)
    label = f"{table}.{column} {operator}"
    if "value" in expression:
        label += f" {expression['value']!r}"
    return label


_AUTHORITATIVE_EMPTY_RESULT_PATTERN = re.compile(
    r"\b(?:missing|incomplete|expired|unassigned|unbooked|unreviewed|"
    r"never|without|no\s+(?:matching\s+)?|not\s+(?:have|having|assigned|"
    r"booked|reviewed|verified|complete)|did\s+any|are\s+there\s+any|"
    r"is\s+there\s+any|orphan(?:ed)?|anomal(?:y|ies))\b",
    re.IGNORECASE,
)


def _filter_leaves(expression: Any) -> list[dict[str, Any]]:
    if not isinstance(expression, dict):
        return []
    if expression.get("boolean") in {"AND", "OR"}:
        conditions = expression.get("conditions")
        if not isinstance(conditions, list):
            return []
        return [
            leaf
            for condition in conditions
            for leaf in _filter_leaves(condition)
        ]
    return [expression]


def _zero_result_is_authoritative(
    generated_query: dict,
    question: str,
) -> bool:
    """Preserve empty results for negative/exclusion and anomaly-search contracts."""
    if generated_query.get("exclude_entities_with_rows_in"):
        return True
    if isinstance(question, str) and _AUTHORITATIVE_EMPTY_RESULT_PATTERN.search(
        question
    ):
        return True
    return any(
        isinstance(term, dict)
        and term.get("operator") in {"IS NULL", "IS MISSING", "NOT IN"}
        for term in _filter_leaves(
            generated_query.get("where_expression")
        )
    )


async def _diagnose_zero_rows(
    generated_query: dict,
    schema_context: dict,
    relationships: list[dict],
) -> dict[str, Any] | None:
    max_probes = 4
    base_query = copy.deepcopy(generated_query)
    base_query["limit"] = 1
    base_query["order_by"] = []
    base_query["ranking_intent"] = "none"
    probes = 0

    async def probe(
        candidate_query: dict,
        relaxed_filters: list[str],
    ) -> dict[str, Any] | None:
        safety_result = run_safety_check(
            candidate_query,
            valid_tables=list(schema_context),
            schema_context=schema_context,
            relationships=relationships,
        )
        validated_query, _ = _build_validated_execution_query(
            candidate_query,
            safety_result,
            schema_context,
            list(schema_context),
            relationships,
        )
        result = await run_query(validated_query)
        if not result.get("results"):
            return None
        message = (
            "No rows matched the original filters. Relaxing "
            + ", ".join(f"`{item}`" for item in relaxed_filters)
            + " would return rows."
        )
        logger.info(
            "zero_row_diagnostic recovered after relaxing %s",
            relaxed_filters,
        )
        return {"message": message, "relaxed_filters": relaxed_filters}

    where_terms = _and_terms(base_query.get("where_expression"))
    for index, term in enumerate(where_terms):
        remaining = where_terms[:index] + where_terms[index + 1:]
        relaxed_query = copy.deepcopy(base_query)
        relaxed_query["where_expression"] = _combine_and_terms(remaining)
        probes += 1
        result = await probe(relaxed_query, [_filter_diagnostic_label(term)])
        if result is not None:
            return result
        if probes >= max_probes:
            return None

    for join_index, join in enumerate(base_query.get("joins", [])):
        if not isinstance(join, dict):
            continue
        conditions = join.get("conditions") or []
        for condition_index, condition in enumerate(conditions):
            if not isinstance(condition, dict):
                continue
            relaxed_query = copy.deepcopy(base_query)
            candidate_join = relaxed_query["joins"][join_index]
            current_conditions = candidate_join.get("conditions") or []
            candidate_join["conditions"] = (
                current_conditions[:condition_index]
                + current_conditions[condition_index + 1:]
            )
            probes += 1
            result = await probe(
                relaxed_query,
                [_filter_diagnostic_label(condition)],
            )
            if result is not None:
                return result
            if probes >= max_probes:
                return None
    return None


async def node_execute(state: GraphState) -> GraphState:
    _ensure_diagnostics(state)
    safety_result = state["safety_check_result"]
    generated_query = state["generated_query"]
    schema_context = state["schema_context"]
    valid_tables = list(schema_context)
    relationships = state.get("schema_relationships", [])
    select_items = generated_query.get("select_items", [])
    try:
        validated_query, columns = _build_validated_execution_query(
            generated_query,
            safety_result,
            schema_context,
            valid_tables,
            relationships,
        )
    except SafetyViolationError as e:
        state["execution_exhausted"] = True
        state["last_execution_error"] = (
            f"Tie-expansion query failed safety validation: {e}"
        )
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "safety_check",
            "reason": state["last_execution_error"],
            "attempts_made": 1,
        }
        return state

    if not columns:
        table = safety_result["table"]
        logger.error(
            "No result columns could be resolved for table '%s' — schema_context missing or empty",
            table,
        )
        state["execution_exhausted"] = True
        state["last_execution_error"] = f"Could not resolve columns for table '{table}'."
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "run_self_fix",
            "reason": state["last_execution_error"],
            "attempts_made": 1,
        }
        return state

    logger.warning(
        "GENERATED_SQL request question=%r\n  SQL: %s\n  PARAMS: %s",
        state["classifier_result"]["rewritten_question"],
        validated_query["sql"],
        validated_query["params"],
    )

    try:
        async def repair_execution_query(
            execution_error: QueryExecutionError,
            attempt: int,
        ) -> dict:
            nonlocal validated_query, columns
            classifier_result = state.get("classifier_result", {})
            original_question = classifier_result.get(
                "original_question", state.get("user_question", "")
            )
            rewritten_question = classifier_result.get(
                "rewritten_question", original_question
            )
            repair_options = {
                "catalog_filters": classifier_result.get("catalog_filters", []),
                "catalog_filter_combiner": classifier_result.get(
                    "catalog_filter_combiner"
                ),
                "resolved_temporal_constraints": classifier_result.get(
                    "resolved_temporal_constraints", []
                ),
                "current_state_filters": classifier_result.get(
                    "current_state_filters", []
                ),
            }
            try:
                repaired_query = await repair_query_after_safety_rejection(
                    state.get("generated_query", generated_query),
                    str(execution_error),
                    rewritten_question,
                    schema_context,
                    state.get("matched_business_rules", []),
                    relationships,
                    original_question,
                    **repair_options,
                )
                repaired_safety_result = run_safety_check(
                    repaired_query,
                    valid_tables=valid_tables,
                    schema_context=schema_context,
                    relationships=relationships,
                )
                repaired_execution_query, repaired_columns = (
                    _build_validated_execution_query(
                        repaired_query,
                        repaired_safety_result,
                        schema_context,
                        valid_tables,
                        relationships,
                    )
                )
                if not repaired_columns:
                    raise QueryGenerationError(
                        "The repaired query plan did not resolve any result columns."
                    )
            except UnanswerableQuestionError as exc:
                state["unanswerable_reasoning"] = exc.reasoning
                state["diagnostics"]["refusal"] = {
                    "code": "UNANSWERABLE_SCHEMA_EVIDENCE",
                    "stage": "execute_repair",
                    "detail": exc.internal_reasoning,
                }
                raise QueryRepairError(
                    f"Repair returned a verified unanswerable verdict: {exc}",
                    terminal=True,
                ) from exc
            except QueryGenerationError as exc:
                code = infrastructure_failure_code(exc.__cause__ or exc)
                if code:
                    raise QueryInfrastructureError(str(exc), code) from exc
                state["diagnostics"]["execution_repairs"].append({
                    "attempt": attempt,
                    "status": "validation_error",
                    "error": str(exc),
                })
                raise QueryRepairError(str(exc)) from exc
            except SafetyViolationError as exc:
                state["diagnostics"]["execution_repairs"].append({
                    "attempt": attempt,
                    "status": "safety_rejected",
                    "error": str(exc),
                    "rule_code": exc.rule_id,
                })
                raise QueryRepairError(
                    f"Repaired query failed safety validation: {exc}"
                ) from exc

            validated_query = repaired_execution_query
            columns = repaired_columns
            state["generated_query"] = repaired_query
            state["safety_check_result"] = repaired_safety_result
            state["diagnostics"]["query_plan"] = {
                key: value for key, value in repaired_query.items()
                if key != "reasoning"
            }
            state["diagnostics"]["generated_sql"] = repaired_safety_result["sql"]
            state["diagnostics"]["execution_repairs"].append({
                "attempt": attempt,
                "status": "validated",
                "error": str(execution_error),
                "generated_sql": repaired_safety_result["sql"],
            })
            return repaired_execution_query

        result = await run_query_with_repair(
            validated_query,
            repair_execution_query,
            executor=run_query,
        )
        generated_query = state.get("generated_query", generated_query)
        
        # --- NEW LOG LINE ADDED HERE ---
        logger.info(
            "TIE_CHECK tie_column=%r user_limit=%s row_limit=%s tie_at_limit=%s",
            validated_query["tie_column"],
            validated_query["user_limit"],
            validated_query["row_limit"],
            result.get("tie_at_limit"),
        )
        # -------------------------------

        result["column_formats"] = validated_query["column_formats"]
        result["suggestions"] = []
        if (
            result.get("row_count") == 0
            and not generated_query.get("group_by")
            and not any(
                isinstance(item, dict) and item.get("aggregate")
                for item in select_items
            )
            and not _zero_result_is_authoritative(
                generated_query,
                state.get("classifier_result", {}).get(
                    "original_question",
                    state.get("user_question", ""),
                ),
            )
        ):
            try:
                diagnostic = await _diagnose_zero_rows(
                    generated_query,
                    schema_context,
                    relationships,
                )
                if diagnostic is not None:
                    result["zero_row_diagnostic"] = diagnostic
            except (QueryExecutionError, QueryGenerationError, SafetyViolationError):
                logger.exception("Node 8 bounded zero-row diagnostic failed")
            try:
                suggestion_rows = await find_value_suggestions(
                    generated_query,
                    schema_context,
                    relationships,
                )
                public_suggestions = []
                stored_plans = []
                for suggestion in suggestion_rows:
                    public_suggestion = {
                        key: suggestion[key]
                        for key in ("column", "original", "suggested")
                    }
                    public_suggestions.append(public_suggestion)
                    try:
                        compiled = build_suggestion_execution_plan(
                            generated_query,
                            schema_context,
                            state.get("schema_relationships", []),
                            suggestion,
                        )
                    except SafetyViolationError:
                        logger.exception(
                            "did_you_mean: corrected query failed safety validation"
                        )
                        continue
                    if compiled is None:
                        continue

                    safe_query = compiled["safety_check_result"]
                    fast_query = {
                        "sql": safe_query["sql"],
                        "params": tuple(safe_query["params"]),
                        "columns": columns,
                        "row_limit": safe_query["row_limit"],
                        "user_limit": safe_query["user_limit"],
                        "count_sql": safe_query.get("count_sql"),
                        "count_params": tuple(safe_query.get("count_params", [])),
                        "tie_column": _pick_tie_column(
                            compiled["query"], columns
                        ),
                        "single_best_with_ties": ranking_policy_for(
                            compiled["query"].get("ranking_intent", "none")
                        )["returns_all_optimal_rows"],
                        "column_formats": validated_query["column_formats"],
                    }
                    stored_plans.append({
                        "suggestion": public_suggestion,
                        "validated_query": fast_query,
                        "generated_query": compiled["query"],
                    })

                if stored_plans:
                    try:
                        tokens = await store_suggestion_plans(
                            state["conversation_id"],
                            stored_plans,
                        )
                        for suggestion, token in zip(
                            [plan["suggestion"] for plan in stored_plans],
                            tokens,
                        ):
                            suggestion["suggestion_id"] = token
                    except Exception:
                        logger.exception(
                            "Could not persist suggestion plans; "
                            "suggestions will require normal query submission."
                        )
                result["suggestions"] = public_suggestions
            except Exception:
                logger.exception("Node 8 value-suggestion lookup failed; returning empty result")
        state["execution_result"] = result
        state["execution_exhausted"] = False
        state["last_execution_error"] = None
    except QueryRepairError as e:
        logger.error("Node 8 stopped after query-repair refusal: %s", e)
        state["execution_exhausted"] = True
        state["last_execution_error"] = str(e)
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "run_self_fix",
            "reason": str(e),
            "attempts_made": 1 + len(state["diagnostics"]["execution_repairs"]),
        }
    except QueryExecutionError as e:
        logger.error("Node 8 execution failed: %s", e)
        _record_infrastructure_failure(state, e, "execute")
        if not infrastructure_failure_code(e):
            state["diagnostics"]["refusal"] = {
                "code": "QUERY_EXECUTION_FAILED",
                "stage": "execute",
                "detail": str(e),
            }
        state["execution_exhausted"] = True
        state["last_execution_error"] = str(e)
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "run_self_fix",
            "reason": str(e),
            "attempts_made": 1,
        }
    return state

# ---------------------------------------------------------------------------
# Node 9: Result Grader
# Blocking, per the locked pipeline plan (Node 9 runs BEFORE Node 10) —
# a result must be graded valid before it is shown to the user.
# ---------------------------------------------------------------------------

async def node_result_grader(state: GraphState) -> GraphState:
    _ensure_diagnostics(state)
    question = state["classifier_result"]["rewritten_question"]
    execution_result = state.get("execution_result", {})
    generated_query = state.get("generated_query", {})
    ranking = ranked_result_metadata(generated_query)
    context_note = None
    if ranking is not None:
        ranking_note = build_ranked_result_note(ranking)
        context_note = (
            f"{context_note}\n{ranking_note}" if context_note else ranking_note
        )

    try:
        verdict = await grade_result(
            question,
            execution_result,
            sql=state["safety_check_result"]["sql"],
            context_note=context_note,
            ranking=ranking,
        )
        state["result_grader_verdict"] = verdict
        state["result_grader_exhausted"] = False

        if not verdict["is_valid"]:
            state["diagnostics"]["refusal"] = {
                "code": "RESULT_GRADING_REJECTED",
                "stage": "result_grader",
                "detail": verdict.get("flag_reason"),
            }
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "result_grader",
                "reason": verdict.get("flag_reason") or "Result graded invalid for the question.",
                "attempts_made": 1,
            }
    except ResultGraderError as e:
        # Grading itself failed (LLM/API error) — cannot confirm correctness,
        # so treat it the same as a failed grade, not as a pass-through.
        logger.error("Node 9 grading failed: %s", e)
        _record_infrastructure_failure(state, e, "result_grader")
        state["diagnostics"]["refusal"] = {
            "code": "RESULT_GRADER_FAILURE",
            "stage": "result_grader",
            "detail": str(e),
        }
        state["result_grader_verdict"] = {
            "is_valid": False,
            "flagged": True,
            "flag_reason": str(e),
        }
        state["result_grader_exhausted"] = True
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "result_grader",
            "reason": str(e),
            "attempts_made": 1,
        }
    return state


# ---------------------------------------------------------------------------
# Node 10: Return Answer
# ---------------------------------------------------------------------------

async def node_return_save(state: GraphState) -> GraphState:
    state["final_answer"] = await build_answer(state)
    verdict = state.get("result_grader_verdict") or {}
    execution_result = state.get("execution_result") or {}
    if (
        verdict.get("is_valid")
        and not state.get("escalated")
        and execution_result
    ):
        query = state.get("generated_query", {})
        store_verified_turn(
            state["conversation_id"],
            columns=execution_result.get("columns", []),
            rows=execution_result.get("results", []),
            metrics=query.get("select_items", []),
            filters=query.get("where_expression"),
            query={
                "table": query.get("table"),
                "joins": query.get("joins", []),
                "group_by": query.get("group_by", []),
            },
        )
    return state

# ---------------------------------------------------------------------------
# Node 11: Human-in-the-Loop Escalation — STUB, escalation.py not built yet.
# ---------------------------------------------------------------------------

async def node_escalation(state: GraphState) -> GraphState:
    result = await escalate(state)
    state["final_answer"] = result["final_answer"]
    return state


# ---------------------------------------------------------------------------
# Conditional routing functions
# ---------------------------------------------------------------------------
def route_after_classify(state: GraphState) -> str:
    if state.get("escalated"):
        return "escalation"
    if state.get("answerable") is False and state.get("unanswerable_reasoning"):
        return "return_save"
    # Casual talk: skip the whole database pipeline.
    if state.get("classifier_result", {}).get("intent") == "chat":
        return "return_save"
    if state.get("classifier_result", {}).get("intent") == "clarification":
        return "return_save"
    return "grade_schema"


def route_after_grade_schema(state: GraphState) -> str:
    if state.get("grade_sample_exhausted"):
        return "escalation"
    if state.get("answerable") is False and state.get("unanswerable_reasoning"):
        return "return_save"
    return "business_rules"


def route_after_generate_query(state: GraphState) -> str:
    if state.get("escalated"):
        return "escalation"
    if not state.get("answerable", True):
        return "return_save"
    return "safety_check"


def route_after_safety_check(state: GraphState) -> str:
    return "escalation" if state.get("escalated") else "execute"


def route_after_execute(state: GraphState) -> str:
    return "escalation" if state.get("execution_exhausted") else "result_grader"


def route_after_result_grader(state: GraphState) -> str:
    verdict = state.get("result_grader_verdict", {})
    if state.get("escalated") or not verdict.get("is_valid", False):
        return "escalation"
    return "return_save"


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("classify", timed_node("classify", node_classify))
    graph.add_node("grade_schema", timed_node("grade_schema", node_grade_schema))
    graph.add_node("business_rules", timed_node("business_rules", node_business_rules))
    graph.add_node("generate_query", timed_node("generate_query", node_generate_query))
    graph.add_node("safety_check", timed_node("safety_check", node_safety_check))
    graph.add_node("execute", timed_node("execute", node_execute))
    graph.add_node("result_grader", timed_node("result_grader", node_result_grader))
    graph.add_node("return_save", timed_node("return_save", node_return_save))
    graph.add_node("escalation", timed_node("escalation", node_escalation))

    graph.set_entry_point("classify")

    graph.add_conditional_edges("classify", route_after_classify, {
        "grade_schema": "grade_schema",
        "return_save": "return_save",
        "escalation": "escalation",
    })
    graph.add_conditional_edges("grade_schema", route_after_grade_schema, {
        "business_rules": "business_rules",
        "return_save": "return_save",
        "escalation": "escalation",
    })
    graph.add_edge("business_rules", "generate_query")
    graph.add_conditional_edges("generate_query", route_after_generate_query, {
        "safety_check": "safety_check",
        "return_save": "return_save",
        "escalation": "escalation",
    })
    graph.add_conditional_edges("safety_check", route_after_safety_check, {
        "execute": "execute",
        "escalation": "escalation",
    })
    graph.add_conditional_edges("execute", route_after_execute, {
        "result_grader": "result_grader",
        "escalation": "escalation",
    })
    graph.add_conditional_edges("result_grader", route_after_result_grader, {
        "return_save": "return_save",
        "escalation": "escalation",
    })
    graph.add_edge("return_save", END)
    graph.add_edge("escalation", END)

    return graph.compile()

_compiled_graph = None


def get_graph():
    """Compiles the graph once and reuses it — cheap to call repeatedly."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


async def run_pipeline(
    user_question: str,
    request_id: str | None = None,
    conversation_id: str | None = None,
    asked_items: list[str] | None = None,
    catalog_filters: list[dict] | None = None,
    widening_unclear_phrase: str | None = None,
    widening_chosen_value: str | None = None,
) -> GraphState:
    """
    Main entry point for the FastAPI /chat route.
    """
    request_id = request_id or str(uuid.uuid4())
    initial_state = new_state(
        user_question,
        request_id,
        conversation_id,
        asked_items,
        catalog_filters,
        widening_unclear_phrase,
        widening_chosen_value,
    )
    initial_state["conversation_context"] = get_conversation_context(
        initial_state["conversation_id"]
    )
    events = start_request()
    graph = get_graph()
    callback_handler = create_callback_handler()
    config: dict[str, Any] = {
        "configurable": {"thread_id": initial_state["conversation_id"]},
        "metadata": {"request_id": request_id},
    }
    if callback_handler is not None:
        config["callbacks"] = [callback_handler]
    final_state = await graph.ainvoke(initial_state, config=config)
    final_state["timings"] = events
    return final_state