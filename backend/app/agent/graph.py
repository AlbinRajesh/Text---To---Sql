"""
graph.py — Wires all 11 nodes into a LangGraph StateGraph.

Built nodes (1, 2, 3+4, 5, 6, 7, 8, 9) call your real implementations.
Nodes not yet built (10, 11) are stub implementations marked clearly below —
replace their bodies once those files are written; the graph wiring itself
won't need to change since the state shape is already defined for them.

Run flow:
  classify -> [preflight refusal] -> return_save -> END
           -> cache_check -> [hit] -> return_save -> END
                           -> [miss] -> grade_schema -> [exhausted] -> escalation -> END
                                                      -> [ok] -> business_rules
                                                                 -> generate_query -> [unanswerable] -> return_save -> END
                                                                                    -> [error] -> escalation -> END
                                                                                    -> [ok] -> safety_check -> [violation] -> escalation -> END
                                                                                                              -> [ok] -> execute -> [exhausted] -> escalation -> END
                                                                                                                                  -> [ok] -> result_grader -> [invalid] -> escalation -> END
                                                                                                                                                            -> [valid] -> return_save -> END
"""

import logging
import hashlib
import json
import uuid
from app.agent.nodes.classifier_rewrite import classify_and_rewrite, ClassificationError

from langgraph.graph import StateGraph, END

from app.agent.state import GraphState, new_state
from app.agent.nodes.cache_check import check_cache
from app.agent.nodes.return_save import build_and_save_answer
from app.agent.nodes.escalation import escalate
from app.agent.nodes.grade_sample import (
    grade_schema_sufficiency,
    GradeSampleError,
    MissingRelationshipError,
    SchemaGraderUnavailableError,
)
from app.agent.nodes.business_rules import find_relevant_rules, get_business_rules_store
from app.agent.nodes.schema_grounding import get_cached_schema, get_cached_relationships
from app.agent.nodes.generate_query import (
    generate_query,
    repair_query_after_safety_rejection,
    QueryGenerationError,
    UnanswerableQuestionError,
    preflight_refusals,
)
from app.agent.nodes.safety_check import (
    REPAIRABLE_SAFETY_RULES,
    run_safety_check,
    SafetyViolationError,
)
from app.agent.nodes.run_self_fix import run_query, QueryExecutionError
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
from app.services.suggestion_store import store_suggestion_plans

logger = logging.getLogger(__name__)


def _current_context_version() -> str:
    context = {
        "schema": get_cached_schema(),
        "relationships": get_cached_relationships(),
        "business_rules": get_business_rules_store().version,
    }
    serialized = json.dumps(context, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Node 1: Classifier + Rewrite
# ---------------------------------------------------------------------------

async def node_classify(state: GraphState) -> GraphState:
    try:
        result = await classify_and_rewrite(state["user_question"])
        state["classifier_result"] = result
    except ClassificationError as e:
        logger.error("Node 1 failed: %s", e)
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
    return state

# ---------------------------------------------------------------------------
# Node 2: Cache Check
# ---------------------------------------------------------------------------

async def node_cache_check(state: GraphState) -> GraphState:
    question = state["classifier_result"]["rewritten_question"]
    state["context_version"] = _current_context_version()
    cached_answer = await check_cache(question, state["context_version"])

    if cached_answer is not None:
        state["cache_hit"] = True
        state["cached_answer"] = cached_answer
        logger.info("Node 2: cache HIT for %r", question)
    else:
        state["cache_hit"] = False
        state["cached_answer"] = None
    return state


# ---------------------------------------------------------------------------
# Node 4 (wraps Node 3 internally via grade_schema_sufficiency's own retry loop)
# ---------------------------------------------------------------------------

async def node_grade_schema(state: GraphState) -> GraphState:
    question = state["classifier_result"]["rewritten_question"]
    try:
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
        state["grade_sample_exhausted"] = False
        state["answerable"] = False
        state["unanswerable_reasoning"] = e.reasoning
    except SchemaGraderUnavailableError as e:
        logger.error("Node 4 grader unavailable: %s", e)
        state["grade_sample_exhausted"] = True
        state["escalated"] = True
        state["escalation_info"] = {
            "node": "grade_sample",
            "reason": f"SERVICE_UNAVAILABLE: {e}",
            "attempts_made": 1,
        }
    except GradeSampleError as e:
        logger.error("Node 4 exhausted retries: %s", e)
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
    question = state["classifier_result"]["rewritten_question"]
    # find_relevant_rules is sync — fine to call directly inside an async node.
    state["matched_business_rules"] = find_relevant_rules(question)
    return state


# ---------------------------------------------------------------------------
# Node 6: Generate Query
# ---------------------------------------------------------------------------

async def node_generate_query(state: GraphState) -> GraphState:
    question = state["classifier_result"]["rewritten_question"]
    schema_context = state["schema_context"]
    matched_rules = state["matched_business_rules"]

    try:
        result = await generate_query(
            question,
            schema_context,
            matched_rules,
            state.get("schema_relationships", []),
            state["classifier_result"]["original_question"],
            request_id=state.get("request_id"),
        )
        state["generated_query"] = result
        state["answerable"] = True
        state["unanswerable_reasoning"] = None
    except UnanswerableQuestionError as e:
        state["answerable"] = False
        state["unanswerable_reasoning"] = e.reasoning
    except QueryGenerationError as e:
        logger.error("Node 6 failed: %s", e)
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
    except SafetyViolationError as e:
        state["safety_violation_reason"] = str(e)
        if e.rule_id not in REPAIRABLE_SAFETY_RULES:
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "safety_check",
                "reason": str(e),
                "attempts_made": 1,
            }
            return state

        state["safety_repair_attempts"] = 1
        classifier_result = state.get("classifier_result", {})
        original_question = classifier_result.get(
            "original_question", state.get("user_question", "")
        )
        rewritten_question = classifier_result.get(
            "rewritten_question", original_question
        )
        try:
            repaired_query = await repair_query_after_safety_rejection(
                query,
                str(e),
                rewritten_question,
                schema_context,
                state.get("matched_business_rules", []),
                relationships,
                original_question,
            )
            state["generated_query"] = repaired_query
            result = run_safety_check(
                repaired_query,
                valid_tables=valid_tables,
                schema_context=schema_context,
                relationships=relationships,
            )
        except SafetyViolationError as retry_error:
            state["safety_violation_reason"] = str(retry_error)
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "safety_check",
                "reason": str(retry_error),
                "attempts_made": 2,
                "repair_attempts": 1,
            }
            return state
        except UnanswerableQuestionError as repair_error:
            state["unanswerable_reasoning"] = repair_error.reasoning
            reason = repair_error.internal_reasoning or str(repair_error)
            logger.warning("Safety-plan repair marked the request unanswerable: %s", reason)
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "safety_check",
                "reason": reason,
                "attempts_made": 2,
                "repair_attempts": 1,
            }
            return state
        except QueryGenerationError as repair_error:
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "safety_check",
                "reason": f"Safety repair failed validation: {repair_error}",
                "attempts_made": 2,
                "repair_attempts": 1,
            }
            return state
        except Exception as repair_error:
            logger.exception("Unexpected error during the safety-plan repair")
            state["escalated"] = True
            state["escalation_info"] = {
                "node": "safety_check",
                "reason": (
                    "Unexpected safety-plan repair failure: "
                    f"{type(repair_error).__name__}: {repair_error}"
                ),
                "attempts_made": 2,
                "repair_attempts": 1,
            }
            return state

    state["safety_check_result"] = result
    state["safety_violation_reason"] = None
    state["escalated"] = False
    return state


# ---------------------------------------------------------------------------
# Node 8: Run + Self-Fix
# Phase 1 scope (per run_self_fix.py's own docstring): no self-fix retry
# loop is implemented in run_query() itself yet, so this node treats a
# single failure as exhausted. Update this once Phase 2 lands.
# ---------------------------------------------------------------------------

def _pick_tie_column(generated_query: dict, columns: list) -> str | None:
    """The first ORDER BY entry that is a selected result column. Only an
    aggregate/alias column is used, because a tie on a plain column is not a
    'top N' ambiguity we can detect from the result rows."""
    aliases = {
        item.get("alias")
        for item in generated_query.get("select_items", [])
        if isinstance(item, dict) and item.get("alias")
    }
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


async def node_execute(state: GraphState) -> GraphState:
    safety_result = state["safety_check_result"]
    generated_query = state["generated_query"]
    schema_context = state["schema_context"]
    table = safety_result["table"]

    select_items = generated_query.get("select_items", [])

    columns = []
    for item in select_items:
        column = item.get("column")
        aggregate = item.get("aggregate")
        alias = item.get("alias")

        if column == "*" and aggregate is None:
            # Plain SELECT * -> expand to the real ordered column list.
            columns.extend(
                col["column"]
                for col in schema_context.get(table, [])
                if isinstance(col, dict) and "column" in col
            )
        elif alias:
            columns.append(alias)
        elif aggregate:
            # Aggregate with no alias, e.g. COUNT(*) -> a readable label.
            columns.append(f"{aggregate.lower()}_{'all' if column == '*' else column}")
        else:
            columns.append(column)

    if not columns:
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

    validated_query = {
        "sql": safety_result["sql"],
        "params": tuple(safety_result["params"]),
        "columns": columns,
        "row_limit": safety_result["row_limit"],
        "user_limit": safety_result["user_limit"],
        "count_sql": safety_result.get("count_sql"),
        "count_params": tuple(safety_result.get("count_params", [])),
        "tie_column": _pick_tie_column(generated_query, columns),
        "column_formats": _column_formats(generated_query, schema_context, table),
    }
    logger.warning(
        "GENERATED_SQL request question=%r\n  SQL: %s\n  PARAMS: %s",
        state["classifier_result"]["rewritten_question"],
        validated_query["sql"],
        validated_query["params"],
    )

    try:
        result = await run_query(validated_query)
        
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
        ):
            try:
                suggestion_rows = await find_value_suggestions(
                    generated_query,
                    schema_context,
                    state.get("schema_relationships", []),
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
    except QueryExecutionError as e:
        logger.error("Node 8 execution failed: %s", e)
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
# a result must be graded valid before it is shown to the user or cached.
# ---------------------------------------------------------------------------

async def node_result_grader(state: GraphState) -> GraphState:
    question = state["classifier_result"]["rewritten_question"]
    execution_result = state.get("execution_result", {})
    generated_query = state.get("generated_query", {})
    ranking = ranked_result_metadata(generated_query)
    context_note = generated_query.get("grader_note")
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
# Node 10: Return + Save
# On a successful, graded-valid answer (or a confirmed-unanswerable verdict),
# caches it for future near-duplicate questions. Never caches escalations —
# those can be caused by transient failures (DB timeout, LLM error), and
# caching them would keep serving a stale failure after the issue clears.
# ---------------------------------------------------------------------------

async def node_return_save(state: GraphState) -> GraphState:
    result = await build_and_save_answer(state)
    state["final_answer"] = result["final_answer"]
    state["saved_to_cache"] = result["saved_to_cache"]
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
    return "cache_check"

def route_after_cache(state: GraphState) -> str:
    return "return_save" if state.get("cache_hit") else "grade_schema"


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
    graph.add_node("cache_check", timed_node("cache_check", node_cache_check))
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
        "cache_check": "cache_check",
        "return_save": "return_save",
        "escalation": "escalation",
    })
    graph.add_conditional_edges("cache_check", route_after_cache, {
        "grade_schema": "grade_schema",
        "return_save": "return_save",
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
) -> GraphState:
    """
    Main entry point for the FastAPI /chat route.
    """
    request_id = request_id or str(uuid.uuid4())
    initial_state = new_state(user_question, request_id, conversation_id)
    events = start_request()
    graph = get_graph()
    final_state = await graph.ainvoke(initial_state)
    final_state["timings"] = events
    return final_state