"""
generate_query.py  (Node 6: Generate the Query — SQL version)

The core "brain" node. Takes the rewritten question plus grounding from
schema_grounding.py (real table/column structure) and business_rules.py
(matched rule definitions), and produces a structured description of a SQL
query — never raw SQL text, only JSON that safety_check.py turns into an
actual parameterized query.

Uses the STRONG model (per the project's cost rule).

Includes one bounded retry on validation failure. This is NOT a fix for a
bug — it's a safety net for a known, confirmed LLM behavior: even with a
well-tuned prompt, the model occasionally (not consistently) produces an
invalid or wrong-shaped response for the same question it usually gets
right (e.g. picking a plain column instead of COUNT(*) for "how many X").
Node 9 (result_grader) catches a wrong-but-valid-shaped answer downstream
regardless, but retrying here first is a better user experience: a 1-2
second delay and a correct answer, instead of every rare miss dead-ending
in an escalation.
"""

import json
import logging
import math
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from app.agent.nodes.schema_grounding import get_ambiguous_names
from app.agent.nodes.calculated_metrics import (
    compile_share_of_rows,
    validate_metric,
    validate_share_condition,
)
from app.agent.search_config import (
    MAX_SEARCH_TERM_LENGTH,
    SEARCH_ROOT_TABLES,
    SEARCHABLE_COLUMNS,
    SearchConfigurationError,
    validate_searchable_columns,
)
from app.agent.relationship_overrides import join_is_fanout_safe
from app.agent.period_aggregation import (
    normalize_period,
    normalize_period_comparison,
    validate_period_sum_column,
)
from app.agent.catalog_resolver import (
    build_catalog_filter_expression,
    catalog_filter_matches_expected,
    is_catalog_filter_predicate,
    validate_catalog_filter_plan,
)
from app.agent.plan_contract import (
    QUERY_PLAN_FIELDS,
    RANKING_INTENTS,
    ranking_policy_for,
)
from app.agent.search_config import normalize_shop_name_term
from datetime import date as _date, timedelta as _timedelta
from app.agent.prompts.generate_query_prompt import (
    build_generate_query_messages,
)
from app.services.llm_service import LLMServiceError, get_llm_service
from app.services.errors import infrastructure_failure_code

_ACTIVE_QUERY_DIAGNOSTICS: ContextVar[Dict[str, Any] | None] = ContextVar(
    "active_query_diagnostics", default=None
)


@contextmanager
def query_diagnostics_scope(diagnostics: Dict[str, Any]):
    token = _ACTIVE_QUERY_DIAGNOSTICS.set(diagnostics)
    try:
        yield
    finally:
        _ACTIVE_QUERY_DIAGNOSTICS.reset(token)

logger = logging.getLogger(__name__)

ALLOWED_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE", "IS NULL", "IS NOT NULL", "BETWEEN"}
ALLOWED_AGGREGATES = {None, "COUNT", "SUM", "AVG", "MIN", "MAX"}
ALLOWED_DIRECTIONS = {"ASC", "DESC"}
ALLOWED_JOIN_TYPES = {"INNER", "LEFT"}
ALLOWED_BOOLEAN_OPERATORS = {"AND", "OR"}
MAX_QUERY_TABLES = 5
MAX_FILTER_DEPTH = 8
MAX_FILTER_TERMS = 64
MAX_USER_LIMIT = 1000
_VALID_ALIAS = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_RATIO_INTENT_PATTERN = re.compile(
    r"%|\b(?:percent(?:age)?|proportion)\b|\bshare\s+of\b",
    re.IGNORECASE,
)
_UNSUPPORTED_RATIO_PATTERN = re.compile(r"\bratio\b", re.IGNORECASE)
_PERCENT_UNIT_PATTERN = re.compile(r"%|\bpercent(?:age)?\b", re.IGNORECASE)
_CURRENCY_UNIT_PATTERN = re.compile(
    r"₹|\b(?:rupees?|rs\.?|inr|currency|amount in)\b", re.IGNORECASE
)
_UNSUPPORTED_SAVINGS_PATTERN = re.compile(
    r"\b(?:save|saves|saving|saved)\b.{0,60}"
    r"(?:"
    r"(?:\b(?:more|greater)\s+than\b|\b(?:at\s+least|above|over)\b)"
    r"\s*(?:₹\s*)?\d[\d,]*(?:\.\d+)?\b"
    r"|(?:\b(?:rupees?|rs\.?|inr)\s*|₹\s*)\d[\d,]*(?:\.\d+)?\b"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:rupees?|rs\.?|inr)\b"
    r")",
    re.IGNORECASE,
)
# Capture the whole phrase so modifiers do not masquerade as joined entities.
_AT_LEAST_ONE_PHRASE_PATTERN = re.compile(
    r"\bat\s+least\s+one\s+"
    r"(?P<phrase>[a-z][a-z0-9]*(?:[\s-]+[a-z][a-z0-9]*){0,5})",
    re.IGNORECASE,
)
# Quoted business names are data values, not existential conditions.
_AT_LEAST_ONE_QUOTED_TEXT_PATTERN = re.compile(
    r'"[^"\r\n]*"|“[^”\r\n]*”|‘[^’\r\n]*’|(?<!\w)\'[^\'\r\n]*\'(?!\w)'
)
_AT_LEAST_ONE_MODIFIER_WORDS = frozenset(
    {"active", "completed", "open", "paid", "pending"}
)
_AT_LEAST_ONE_PHRASE_STOP_WORDS = frozenset(
    {
        "and", "are", "as", "by", "for", "has", "have", "in", "is", "of",
        "on", "that", "the", "to", "was", "were", "with",
    }
)
_PLAN_ASSIGNMENT_PATTERN = re.compile(
    r"\bplan\b.{0,24}\bassigned\b|\bassigned\b.{0,24}\bplan\b",
    re.IGNORECASE,
)
_SQL_INJECTION_PATTERN = re.compile(
    r"(?:;\s*(?:DROP|ALTER|TRUNCATE|DELETE|INSERT|UPDATE)\b)|(?:--\s*$)|(?:/\*.*?\*/)",
    re.IGNORECASE,
)
_INTEGER_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year",
}
_DECIMAL_TYPES = {"decimal", "numeric"}
_FLOAT_TYPES = {"float", "double", "real"}
_COUNT_INTENT_PATTERN = re.compile(
    r"\b(?:how many|number of|count|counting|total number of)\b",
    re.IGNORECASE,
)
_EXPLICIT_EMPTY_GROUP_INTENT_PATTERN = re.compile(
    r"\b(?:each|every|all)\s+(?:(?:of\s+)?(?:the\s+)?)?[a-z][a-z_-]*\b",
    re.IGNORECASE,
)


_DISTINCT_VALUE_COUNT_INTENT_PATTERN = re.compile(
    r"\b(?:unique|distinct|different)\b", re.IGNORECASE
)

MAX_ATTEMPTS = 3  # Initial plan plus at most two validation repairs.


# Debug switch: set LOG_RETRY_PROMPT=1 in the environment to log what the model
# is sent on a retry. Off by default so production logs never get prompt text.
_LOG_RETRY_PROMPT = os.getenv("LOG_RETRY_PROMPT", "").strip().lower() in {"1", "true", "yes"}

class QueryGenerationError(Exception):
    """Raised when the LLM's response is malformed or fails validation, even after retrying."""

    def __init__(
        self,
        message: str = "",
        *,
        rule_id: str | None = None,
        stage: str = "generator",
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.stage = stage


class UnanswerableQuestionError(Exception):
    """
    Raised when the LLM determined the question can't be answered with the
    available schema and verified relationships. This is an honest
    "no", not a system bug — route it to the user as a clear message.
    Raised only after structured missing-resource evidence is verified.

    `reasoning` is the user-facing text. `internal_reasoning` is the model's
    raw explanation: for logs only, never shown to users.
    """

    def __init__(self, reasoning: str, *, internal_reasoning: str | None = None) -> None:
        self.reasoning = reasoning
        self.internal_reasoning = internal_reasoning
        super().__init__(reasoning)


def preflight_refusals(
    rewritten_question: str,
    original_question: str | None = None,
) -> None:
    """Refuse questions with known unsupported temporal or derived-value semantics."""
    questions = (rewritten_question, original_question or "")

    # 0. Malicious SQL injection or DDL / stacked statement attempt
    if any(_SQL_INJECTION_PATTERN.search(question) for question in questions):
        raise UnanswerableQuestionError(
            "SQL injection or destructive statement patterns are not permitted."
        )



_DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_OF_DAY_TYPES = {"timestamp", "datetime"}


def _is_date_only(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_ONLY_PATTERN.match(value):
        return False
    try:
        _date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _next_day(value: str) -> str:
    return (_date.fromisoformat(value) + _timedelta(days=1)).isoformat()


def _expand_date_only_bounds(
    operator: str, value: Any, data_type: str | None
) -> list[tuple[str, Any]] | None:
    """Cover whole days on timestamp columns; return None when no change is needed."""
    if not isinstance(data_type, str) or data_type.lower() not in _TIME_OF_DAY_TYPES:
        return None
    if operator == "BETWEEN" and isinstance(value, list) and len(value) == 2:
        low, high = value
        if not _is_date_only(high):
            return None
        return [(">=", low), ("<", _next_day(high))]
    if not _is_date_only(value):
        return None
    if operator == "<=":
        return [("<", _next_day(value))]
    if operator == ">":
        return [(">=", _next_day(value))]
    if operator == "=":
        return [(">=", value), ("<", _next_day(value))]
    return None

def _normalize_filter_expression(
    expression: Any,
    schema_context: Dict[str, Any],
    query_tables: set[str],
    *,
    aggregate_filters: bool = False,
    metric_aliases: set[str] | None = None,
) -> Dict[str, Any] | None:
    """Validate nested boolean predicates and resolve every column to a table."""
    if expression is None:
        return None

    term_count = 0

    def visit(node: Any, depth: int) -> Dict[str, Any]:
        nonlocal term_count
        if depth > MAX_FILTER_DEPTH:
            raise QueryGenerationError(
                f"Filter expression exceeds maximum depth {MAX_FILTER_DEPTH}."
            )
        if not isinstance(node, dict):
            raise QueryGenerationError("Each filter expression must be a JSON object.")

        boolean = node.get("boolean")
        if boolean is not None:
            if set(node) != {"boolean", "conditions"}:
                raise QueryGenerationError(
                    "Boolean filters may contain only 'boolean' and 'conditions'; "
                    "SQL and subquery expressions are not supported."
                )
            conditions = node.get("conditions")
            if (
                boolean not in ALLOWED_BOOLEAN_OPERATORS
                or not isinstance(conditions, list)
                or not conditions
            ):
                raise QueryGenerationError(
                    "Boolean filter groups need AND/OR and at least one condition."
                )
            if len(conditions) == 1:
                return visit(conditions[0], depth + 1)
            return {
                "boolean": boolean,
                "conditions": [visit(condition, depth + 1) for condition in conditions],
            }

        metric_name = node.get("metric")
        metric_alias = node.get("metric_alias")
        if metric_alias is not None:
            declared_alias = next(
                (
                    alias
                    for alias in metric_aliases or set()
                    if isinstance(metric_alias, str)
                    and alias.casefold() == metric_alias.casefold()
                ),
                None,
            )
            if (
                not aggregate_filters
                or declared_alias is None
                or set(node) - {"metric_alias", "operator", "value"}
            ):
                raise QueryGenerationError(
                    "A metric_alias filter must reference a declared isolated metric in HAVING."
                )
            term_count += 1
            if term_count > MAX_FILTER_TERMS:
                raise QueryGenerationError(
                    f"Filter expression exceeds maximum of {MAX_FILTER_TERMS} conditions."
                )
            operator = node.get("operator")
            if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
                raise QueryGenerationError(f"Disallowed filter operator {operator!r}.")
            normalized_alias: Dict[str, Any] = {
                "metric_alias": declared_alias,
                "operator": operator,
            }
            if operator not in {"IS NULL", "IS NOT NULL"}:
                value = node.get("value")
                values = value if operator in {"IN", "NOT IN", "BETWEEN"} else [value]
                if (
                    not isinstance(values, list)
                    or not values
                    or (operator == "BETWEEN" and len(values) != 2)
                    or any(isinstance(item, (dict, list, bool)) for item in values)
                ):
                    raise QueryGenerationError(
                        f"{operator} requires finite numeric isolated-metric values."
                    )
                try:
                    numeric_values = [Decimal(str(item)) for item in values]
                    if any(not number.is_finite() for number in numeric_values):
                        raise InvalidOperation
                except (InvalidOperation, ValueError, TypeError) as exc:
                    raise QueryGenerationError(
                        "Isolated-metric filters require finite numeric values."
                    ) from exc
                normalized_alias["value"] = (
                    numeric_values
                    if operator in {"IN", "NOT IN", "BETWEEN"}
                    else numeric_values[0]
                )
            return normalized_alias

        if metric_name is not None:
            allowed_metric_keys = {"metric", "operator", "value"}
            if aggregate_filters:
                allowed_metric_keys.add("aggregate")
            if set(node) - allowed_metric_keys:
                raise QueryGenerationError(
                    "Calculated-metric filters may contain only a registered "
                    "metric, an allowed operator, and a scalar value."
                )
            try:
                validate_metric(metric_name, schema_context, query_tables)
            except ValueError as exc:
                raise QueryGenerationError(str(exc)) from exc
            term_count += 1
            if term_count > MAX_FILTER_TERMS:
                raise QueryGenerationError(
                    f"Filter expression exceeds maximum of {MAX_FILTER_TERMS} conditions."
                )
            operator = node.get("operator")
            if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
                raise QueryGenerationError(f"Disallowed filter operator {operator!r}.")
            normalized_metric: Dict[str, Any] = {
                "metric": metric_name,
                "operator": operator,
            }
            if aggregate_filters:
                aggregate = node.get("aggregate")
                if aggregate not in {"SUM", "AVG", "MIN", "MAX"}:
                    raise QueryGenerationError(
                        f"Disallowed HAVING aggregate {aggregate!r} for a calculated metric."
                    )
                normalized_metric["aggregate"] = aggregate
            if operator not in {"IS NULL", "IS NOT NULL"}:
                value = node.get("value")
                values = value if operator in {"IN", "NOT IN", "BETWEEN"} else [value]
                if (
                    not isinstance(values, list)
                    or not values
                    or (operator == "BETWEEN" and len(values) != 2)
                    or any(isinstance(item, (dict, list, bool)) for item in values)
                ):
                    raise QueryGenerationError(
                        f"{operator} requires finite numeric scalar value(s)."
                    )
                try:
                    converted = [Decimal(str(item)) for item in values]
                    if any(not number.is_finite() for number in converted):
                        raise InvalidOperation
                except (InvalidOperation, ValueError, TypeError) as exc:
                    raise QueryGenerationError(
                        "Calculated-metric filters require finite numeric values."
                    ) from exc
                normalized_metric["value"] = (
                    converted if operator in {"IN", "NOT IN", "BETWEEN"}
                    else converted[0]
                )
            return normalized_metric

        allowed_keys = {"table", "column", "operator", "value", "aggregate"}
        if set(node) - allowed_keys:
            raise QueryGenerationError(
                "Filter predicates may contain only validated column conditions; "
                "SQL and subquery expressions are not supported."
            )

        term_count += 1
        if term_count > MAX_FILTER_TERMS:
            raise QueryGenerationError(
                f"Filter expression exceeds maximum of {MAX_FILTER_TERMS} conditions."
            )
        owner, column = _resolve_column(
            node,
            schema_context,
            query_tables,
            allow_star=aggregate_filters and node.get("column") == "*",
        )
        operator = node.get("operator")
        if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
            raise QueryGenerationError(f"Disallowed filter operator {operator!r}.")

        normalized: Dict[str, Any] = {
            "table": owner,
            "column": column,
            "operator": operator,
        }
        if aggregate_filters:
            aggregate = node.get("aggregate")
            if aggregate not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
                raise QueryGenerationError(
                    f"Disallowed HAVING aggregate {aggregate!r}."
                )
            normalized["aggregate"] = aggregate
        if operator not in {"IS NULL", "IS NOT NULL"}:
            value = node.get("value")
            if operator in {"IN", "NOT IN", "BETWEEN"}:
                expected_length = 2 if operator == "BETWEEN" else None
                if not isinstance(value, list) or not value or (
                    expected_length is not None and len(value) != expected_length
                ):
                    raise QueryGenerationError(
                        f"{operator} requires a "
                        f"{'[low, high]' if expected_length else 'non-empty list'} value."
                    )
            elif isinstance(value, (dict, list)):
                raise QueryGenerationError(
                    f"{operator} requires a scalar value."
                )
            data_type = _column_data_type(schema_context, owner, column)
            if operator in {"IN", "NOT IN", "BETWEEN"}:
                value = [
                    _coerce_filter_value(
                        item,
                        data_type,
                        table=owner,
                        column=column,
                    )
                    for item in value
                ]
            else:
                value = _coerce_filter_value(
                    value,
                    data_type,
                    table=owner,
                    column=column,
                )
            expanded = (
                None
                if aggregate_filters
                else _expand_date_only_bounds(operator, value, data_type)
            )
            if expanded is not None:
                conditions = [
                    {
                        "table": owner,
                        "column": column,
                        "operator": expanded_operator,
                        "value": bound,
                    }
                    for expanded_operator, bound in expanded
                ]
                if len(conditions) == 1:
                    return conditions[0]
                return {"boolean": "AND", "conditions": conditions}
            normalized["value"] = value
        return normalized

    return visit(expression, 0)


def _validate_booking_city_governance(
    where_expression: Dict[str, Any] | None,
    group_by: List[Dict[str, Any]],
    joins: List[Dict[str, Any]] | None = None,
) -> None:
    if any(
        item.get("table") == "bookings" and item.get("column") == "city"
        for item in group_by
        if isinstance(item, dict)
    ):
        raise QueryGenerationError(
            "Do not GROUP BY bookings.city; it is unvalidated free text. "
            "Use shop_address.city with shop_address.is_default = 1 for "
            "booking/store city analysis.",
            rule_id="booking_city_governance",
        )
    filtered_terms = _expression_leaves(where_expression)
    for join in joins or []:
        if isinstance(join, dict):
            for condition in join.get("conditions") or []:
                filtered_terms.extend(_expression_leaves(condition))
    if any(
        item.get("table") == "bookings" and item.get("column") == "city"
        for item in filtered_terms
    ):
        raise QueryGenerationError(
            "Do not filter WHERE bookings.city; it is unvalidated free text. "
            "Use shop_address.city with shop_address.is_default = 1 for "
            "booking/store city filters.",
            rule_id="booking_city_governance",
        )


def _expression_uses_metric(
    expression: Dict[str, Any] | None,
    metric_name: str,
) -> bool:
    return any(
        leaf.get("metric") == metric_name
        for leaf in _expression_leaves(expression)
    )

def _is_single_primary_key(
    schema_context: Dict[str, Any], table: str, column: str
) -> bool:
    primary_keys = [
        info["column"]
        for info in schema_context.get(table, [])
        if isinstance(info, dict) and info.get("key") == "PRI"
    ]
    return len(primary_keys) == 1 and column == primary_keys[0]

def _apply_savings_policy(
    where_expression: Dict[str, Any] | None,
    select_items: List[Dict[str, Any]],
    having_expression: Dict[str, Any] | None,
    order_by: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    """Apply the fixed eligibility rule whenever the savings metric is used."""
    uses_savings = _query_uses_metric(
        "savings", select_items, where_expression, having_expression, order_by
    )
    if not uses_savings:
        return where_expression
    required = [
        {"metric": "final_price", "operator": ">", "value": Decimal("0")},
        {"metric": "savings", "operator": ">", "value": Decimal("0")},
    ]
    if where_expression is None:
        return {"boolean": "AND", "conditions": required}
    return {
        "boolean": "AND",
        "conditions": [where_expression, *required],
    }


def _query_uses_metric(
    metric_name: str,
    select_items: List[Dict[str, Any]],
    where_expression: Dict[str, Any] | None,
    having_expression: Dict[str, Any] | None,
    order_by: List[Dict[str, Any]],
) -> bool:
    return (
        any(item.get("metric") == metric_name for item in select_items)
        or _expression_uses_metric(where_expression, metric_name)
        or _expression_uses_metric(having_expression, metric_name)
        or any(order.get("metric") == metric_name for order in order_by)
    )


def _validate_metric_filter_units(
    where_expression: Dict[str, Any] | None,
    having_expression: Dict[str, Any] | None,
    question_text: str,
    schema_context: Dict[str, Any],
    query_tables: set[str],
) -> None:
    requested_unit = (
        "percentage"
        if _PERCENT_UNIT_PATTERN.search(question_text)
        else "currency"
        if _CURRENCY_UNIT_PATTERN.search(question_text)
        else None
    )
    if requested_unit is None:
        return
    for expression in (where_expression, having_expression):
        for leaf in _expression_leaves(expression):
            metric_name = leaf.get("metric")
            if metric_name is None:
                continue
            try:
                definition = validate_metric(metric_name, schema_context, query_tables)
            except ValueError as exc:
                raise QueryGenerationError(
                    f"Unsupported calculated metric {metric_name!r} in a filter."
                ) from exc
            if definition.unit != requested_unit:
                raise QueryGenerationError(
                    f"A {requested_unit} threshold cannot filter a "
                    f"{definition.unit} calculated metric."
                )


def _has_percentage_metric(
    select_items: List[Dict[str, Any]],
    schema_context: Dict[str, Any],
    query_tables: set[str],
) -> bool:
    for item in select_items:
        metric_name = item.get("metric")
        if metric_name == "share_of_rows":
            return True
        if metric_name is not None:
            try:
                if validate_metric(
                    metric_name, schema_context, query_tables
                ).unit == "percentage":
                    return True
            except ValueError:
                continue
    return False


def _coerce_filter_value(
    value: Any,
    data_type: str | None,
    *,
    table: str,
    column: str,
) -> Any:
    """Convert numeric filter literals to the type declared by the schema."""
    if not isinstance(data_type, str):
        return value
    normalized_type = data_type.lower()
    if normalized_type not in _INTEGER_TYPES | _DECIMAL_TYPES | _FLOAT_TYPES:
        return value
    if isinstance(value, bool):
        raise QueryGenerationError(
            f"Boolean value is invalid for numeric column {table}.{column}."
        )
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        if normalized_type in _INTEGER_TYPES:
            if number != number.to_integral_value():
                raise InvalidOperation
            return int(number)
        if normalized_type in _DECIMAL_TYPES:
            return number
        converted = float(number)
        if not math.isfinite(converted):
            raise InvalidOperation
        return converted
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise QueryGenerationError(
            f"Filter value {value!r} is not valid for numeric column "
            f"{table}.{column} ({data_type})."
        ) from exc


def _column_data_type(
    schema_context: Dict[str, Any], table: str, column: str
) -> str | None:
    for entry in schema_context.get(table, []):
        if isinstance(entry, dict) and entry.get("column") == column:
            data_type = entry.get("data_type")
            return data_type if isinstance(data_type, str) else None
    return None


def _expand_plain_wildcard_select_items(
    select_items: List[Dict[str, Any]],
    query_tables: set[str],
    schema_context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Expand row-list wildcards before the safety layer builds SQL."""
    expanded: List[Dict[str, Any]] = []
    used_aliases: set[str] = set()
    sensitive_columns = {
        "password",
        "remember_token",
        "fcm_token",
        "firebase_uid",
        "facebook_id",
    }

    for item in select_items:
        if not isinstance(item, dict):
            raise QueryGenerationError("Each select_item must be a JSON object.")
        if (
            item.get("column") != "*"
            or item.get("aggregate") is not None
        ):
            alias = item.get("alias")
            if isinstance(alias, str):
                used_aliases.add(alias)
            expanded.append(item)
            continue

        if len(query_tables) != 1:
            raise QueryGenerationError(
                "Plain wildcard row selections are only supported for a single table."
            )
        table = next(iter(query_tables))
        columns = [
            entry.get("column")
            for entry in schema_context.get(table, [])
            if isinstance(entry, dict)
            and isinstance(entry.get("column"), str)
            and entry["column"].lower() not in sensitive_columns
        ]
        if not columns:
            raise QueryGenerationError(
                f"No safe selectable columns are available for table '{table}'."
            )
        for column in columns:
            alias = column
            if alias in used_aliases:
                alias = f"{table}_{column}"
            used_aliases.add(alias)
            expanded.append({
                "table": table,
                "column": column,
                "aggregate": None,
                "distinct": False,
                "alias": alias,
            })

    return expanded


def _expression_leaves(expression: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    if expression is None:
        return []
    if "boolean" in expression:
        return [
            leaf
            for condition in expression["conditions"]
            for leaf in _expression_leaves(condition)
        ]
    return [expression]


def _table_name_forms(table: str) -> set[str]:
    folded = table.casefold()
    forms = {folded}
    if folded.endswith("ies"):
        forms.add(f"{table[:-3]}y".casefold())
    elif folded.endswith("s") and not folded.endswith("ss"):
        forms.add(table[:-1].casefold())
    else:
        forms.add(f"{table}s".casefold())
    # "shop_details" is the table for "shops": also match the bare entity word.
    if folded.endswith("_details"):
        forms.update(_table_name_forms(table[: -len("_details")]))
    return forms


def _resolve_at_least_one_joined_entity(
    question: str | None,
    joins: List[Dict[str, Any]],
    *,
    reject_unresolved: bool = True,
    base_table: str | None = None,
) -> tuple[Dict[str, Any], str] | None:
    if not question:
        return None

    quoted_spans = [
        quoted.span() for quoted in _AT_LEAST_ONE_QUOTED_TEXT_PATTERN.finditer(question)
    ]
    match = next(
        (
            candidate
            for candidate in _AT_LEAST_ONE_PHRASE_PATTERN.finditer(question)
            if not any(start <= candidate.start() < end for start, end in quoted_spans)
        ),
        None,
    )
    if not match:
        return None

    phrase_words = [
        word.casefold()
        for word in re.findall(r"[a-z][a-z0-9]*", match.group("phrase"), re.IGNORECASE)
    ]
    entity_words = [
        word
        for word in phrase_words
        if word not in _AT_LEAST_ONE_MODIFIER_WORDS
        and word not in _AT_LEAST_ONE_PHRASE_STOP_WORDS
    ]
    matched_entities = [
        (
            word_index,
            join,
            word,
        )
        for word_index, word in enumerate(entity_words)
        for join in joins
        if word in _table_name_forms(join["table"])
    ]
    if not matched_entities:
        if base_table and any(
            word in _table_name_forms(base_table) for word in entity_words
        ):
            return None
        if reject_unresolved:
            raise QueryGenerationError(
                "An 'at least one' condition must identify exactly one joined entity table."
            )
        return None

    head_index = max(item[0] for item in matched_entities)
    head_candidates = [
        (join, word)
        for word_index, join, word in matched_entities
        if word_index == head_index
    ]
    if len(head_candidates) != 1:
        if reject_unresolved:
            raise QueryGenerationError(
                "An 'at least one' condition must identify exactly one joined entity table."
            )
        return None
    return head_candidates[0]


def _enforce_at_least_one_joined_entity(
    question: str | None,
    where_expression: Dict[str, Any] | None,
    joins: List[Dict[str, Any]],
    schema_context: Dict[str, Any],
    base_table: str | None = None,
) -> Dict[str, Any] | None:
    resolved_entity = _resolve_at_least_one_joined_entity(
        question, joins, base_table=base_table
    )
    if resolved_entity is None:
        return where_expression

    child_join, entity = resolved_entity
    if child_join["join_type"] == "INNER":
        return where_expression

    child_table = child_join["table"]
    primary_keys = [
        column["column"]
        for column in schema_context.get(child_table, [])
        if isinstance(column, dict) and column.get("key") == "PRI"
    ]
    if len(primary_keys) != 1:
        raise QueryGenerationError(
            f"An 'at least one {entity}' condition requires a single-column "
            f"primary key on '{child_table}'."
        )

    existence_predicate = {
        "table": child_table,
        "column": primary_keys[0],
        "operator": "IS NOT NULL",
    }
    if where_expression is None:
        return existence_predicate
    if (
        where_expression.get("table") == child_table
        and where_expression.get("column") == primary_keys[0]
        and where_expression.get("operator") == "IS NOT NULL"
    ):
        return where_expression
    if where_expression.get("boolean") == "AND":
        conditions = where_expression["conditions"]
        if existence_predicate in conditions:
            return where_expression
        return {
            **where_expression,
            "conditions": [*conditions, existence_predicate],
        }
    return {
        "boolean": "AND",
        "conditions": [where_expression, existence_predicate],
    }


def _validate_active_offer_plan(
    query: Dict[str, Any],
    schema_context: Dict[str, Any],
    matched_business_rules: List[Dict[str, Any]] | None = None,
    current_state_filters: List[Dict[str, Any]] | None = None,
) -> None:
    if not any(
        isinstance(rule, dict) and rule.get("term") == "active_offer_status"
        for rule in matched_business_rules or []
    ):
        return

    query_tables = {query.get("table")}
    query_tables.update(
        join.get("table")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    )
    if "offer" not in query_tables:
        raise QueryGenerationError(
            "Active-offer questions must use the verified offer table."
        )

    predicates = _expression_leaves(query.get("where_expression"))
    for join in query.get("joins", []):
        if not isinstance(join, dict):
            continue
        for condition in join.get("conditions") or []:
            if isinstance(condition, dict):
                predicates.extend(_expression_leaves(condition))
    if not any(
        predicate.get("table") == "offer"
        and predicate.get("column") == "status"
        and predicate.get("operator") == "="
        and str(predicate.get("value")) == "1"
        for predicate in predicates
    ):
        raise QueryGenerationError(
            "Active offers must be filtered using offer.status = 1."
        )
    if (
        not any(
            isinstance(item, dict)
            and item.get("entity") in {"shop_details", "shops"}
            and item.get("state") == "active"
            for item in current_state_filters or []
        )
        and any(
            predicate.get("table") == "shop_details"
            and predicate.get("column") == "status"
            for predicate in predicates
        )
    ):
        raise QueryGenerationError(
            "Active-offer questions must not apply a shop_details.status filter "
            "unless the question also asks for active shops."
        )
    for predicate in predicates:
        if predicate.get("table") != "offer":
            continue
        data_type = _column_data_type(
            schema_context, "offer", predicate.get("column", "")
        )
        if isinstance(data_type, str) and data_type.casefold() in {
            "date", "datetime", "timestamp", "time",
        }:
            raise QueryGenerationError(
                "Active-offer questions must not apply an offer date condition."
            )

    review_count_requested = (
        query.get("count_entity")
        == {"table": "reviews", "column": "id"}
        or any(
            isinstance(item, dict)
            and item.get("aggregate") == "COUNT"
            and item.get("table") == "reviews"
            for item in query.get("select_items", [])
        )
    )
    if review_count_requested:
        review_counts = [
            item for item in query.get("select_items", [])
            if item.get("aggregate") == "COUNT"
        ]
        if (
            query.get("table") != "reviews"
            or len(review_counts) != 1
            or review_counts[0].get("table") != "reviews"
            or review_counts[0].get("column") != "id"
            or not review_counts[0].get("distinct")
            or query.get("count_entity")
            != {"table": "reviews", "column": "id"}
        ):
            raise QueryGenerationError(
                "Review counts for shops with active offers must use "
                "COUNT(DISTINCT reviews.id) from the reviews base table."
            )

def _business_semantic_contracts(
    matched_business_rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [
        contract
        for rule in matched_business_rules
        if isinstance(rule, dict)
        for contract in [
            rule.get("query_logic", {}).get("semantic_contract")
            if isinstance(rule.get("query_logic"), dict)
            else None
        ]
        if isinstance(contract, dict)
    ]


def _select_business_semantic_contracts(
    question: str,
    matched_business_rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    contracts = _business_semantic_contracts(matched_business_rules)
    preferred_kind = (
        "count_entities_by_group_aggregate_threshold"
        if _COUNT_INTENT_PATTERN.search(question)
        else "list_entities_by_group_aggregate_threshold"
    )
    preferred = [
        contract for contract in contracts
        if contract.get("kind") == preferred_kind
    ]
    return preferred or contracts


def _threshold_from_question(
    question: str, contract: Dict[str, Any]
) -> float:
    try:
        match = re.search(contract["threshold_pattern"], question, re.IGNORECASE)
    except (KeyError, re.error) as exc:
        raise QueryGenerationError(
            "Business rule contains an invalid threshold pattern."
        ) from exc
    if match is None:
        raise UnanswerableQuestionError(
            "Please specify the numeric rating threshold so I can calculate the result."
        )
    try:
        threshold = float(match.group(1))
    except (IndexError, ValueError) as exc:
        raise QueryGenerationError(
            "Business rule threshold pattern did not capture a valid number."
        ) from exc
    if not math.isfinite(threshold):
        raise UnanswerableQuestionError("The requested threshold must be a finite number.")
    return threshold


def _build_business_contract_query(
    question: str,
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    threshold = _threshold_from_question(question, contract)
    source_table = contract["source_table"]
    entity_table = contract["entity_table"]
    entity_key = contract["entity_key"]
    relationship_name = contract["relationship"]
    measure_column = contract["measure_column"]
    aggregate = contract["aggregate"]
    operator = contract["operator"]

    if source_table not in schema_context or entity_table not in schema_context:
        raise UnanswerableQuestionError(
            "The required source or entity table is not present in the verified schema."
        )
    entity_columns = schema_context.get(entity_table, [])
    entity_key_info = next(
        (
            column
            for column in entity_columns
            if isinstance(column, dict) and column.get("column") == entity_key
        ),
        None,
    )
    if not isinstance(entity_key_info, dict) or entity_key_info.get("key") != "PRI":
        raise UnanswerableQuestionError(
            "The entity identifier required by this metric is not a verified primary key."
        )
    measure_info = next(
        (
            column
            for column in schema_context.get(source_table, [])
            if isinstance(column, dict) and column.get("column") == measure_column
        ),
        None,
    )
    measure_type = measure_info.get("data_type") if isinstance(measure_info, dict) else None
    if not isinstance(measure_type, str) or measure_type.lower() not in {
        "tinyint",
        "smallint",
        "mediumint",
        "int",
        "integer",
        "bigint",
        "decimal",
        "numeric",
        "float",
        "double",
        "real",
    }:
        raise UnanswerableQuestionError(
            "The measure required by this metric is not a numeric column in the verified schema."
        )
    is_list_contract = (
        contract["kind"] == "list_entities_by_group_aggregate_threshold"
    )
    if is_list_contract:
        label_column = contract["entity_label_column"]
        label_info = next(
            (
                column
                for column in entity_columns
                if isinstance(column, dict) and column.get("column") == label_column
            ),
            None,
        )
        review_key = contract["count_column"]
        review_key_info = next(
            (
                column
                for column in schema_context[source_table]
                if isinstance(column, dict) and column.get("column") == review_key
            ),
            None,
        )
        if not isinstance(label_info, dict):
            raise UnanswerableQuestionError(
                "The entity label required by this metric is not present in the verified schema."
            )
        if not isinstance(review_key_info, dict) or review_key_info.get("key") != "PRI":
            raise UnanswerableQuestionError(
                "The review identifier required by this metric is not a verified primary key."
            )

    relationship = next(
        (
            edge
            for edge in relationships or []
            if edge.get("constraint_name") == relationship_name
            and edge.get("table") == source_table
            and edge.get("referenced_table") == entity_table
        ),
        None,
    )
    if (
        relationship is None
        or relationship.get("cardinality") != "many_to_one"
        or relationship.get("verification")
        not in {"declared_foreign_key", "reviewed_manual_query"}
        or not any(
            pair.get("column") == contract.get("relationship_column")
            and pair.get("referenced_column") == entity_key
            for pair in relationship.get("column_pairs", [])
            if isinstance(pair, dict)
        )
    ):
        raise UnanswerableQuestionError(
            "The reviewed relationship needed to calculate this metric is unavailable."
        )

    query = {
        "answerable": True,
        "reasoning": (
            f"Count {entity_table} entities after grouping {source_table} rows by "
            f"{entity_key} and applying {aggregate}({measure_column}) {operator} {threshold}."
        ),
        "table": source_table,
        "joins": [{
            "table": entity_table,
            "join_type": "INNER",
            "relationship": {
                "constraint_name": relationship_name,
                "table": source_table,
                "referenced_table": entity_table,
            },
        }],
        "select_items": [{
            "table": source_table,
            "column": "*",
            "aggregate": "COUNT",
            "distinct": False,
            "alias": "qualifying_entity_count",
        }],
        "where_expression": None,
        "group_by": [{"table": entity_table, "column": entity_key}],
        "having_expression": {
            "aggregate": aggregate,
            "table": source_table,
            "column": measure_column,
            "operator": operator,
            "value": threshold,
        },
        "count_groups": True,
        "count_entity": None,
        "order_by": [],
    }
    if is_list_contract:
        query.update({
            "select_items": [
                {
                    "table": entity_table,
                    "column": entity_key,
                    "aggregate": None,
                    "alias": "shop_id",
                },
                {
                    "table": entity_table,
                    "column": label_column,
                    "aggregate": None,
                    "alias": "shop_name",
                },
                {
                    "table": source_table,
                    "column": measure_column,
                    "aggregate": aggregate,
                    "alias": contract["measure_alias"],
                },
                {
                    "table": source_table,
                    "column": review_key,
                    "aggregate": "COUNT",
                    "distinct": True,
                    "alias": contract["count_alias"],
                },
            ],
            "group_by": [
                {"table": entity_table, "column": entity_key},
                {"table": entity_table, "column": label_column},
            ],
            "count_groups": False,
            "count_entity": {"table": source_table, "column": review_key},
            "order_by": [{
                "column": contract["measure_alias"],
                "direction": "DESC",
            }],
        })
    return query


def _validate_semantic_contract(
    query: Dict[str, Any],
    contracts: List[Dict[str, Any]],
    question: str,
) -> None:
    for contract in contracts:
        expected_threshold = _threshold_from_question(question, contract)
        is_list_contract = (
            contract["kind"] == "list_entities_by_group_aggregate_threshold"
        )
        expected_group = {
            "table": contract["entity_table"],
            "column": contract["entity_key"],
        }
        expected_groups = [expected_group]
        expected_count_entity = None
        if is_list_contract:
            label_column = contract["entity_label_column"]
            expected_groups.append({
                "table": contract["entity_table"],
                "column": label_column,
            })
            expected_count_entity = {
                "table": contract["source_table"],
                "column": contract["count_column"],
            }
        having = _expression_leaves(query.get("having_expression"))
        if (
            query.get("table") != contract["source_table"]
            or query.get("count_groups") != (not is_list_contract)
            or len(query.get("joins", [])) != 1
            or query["joins"][0].get("table") != contract["entity_table"]
            or query["joins"][0].get("join_type") != "INNER"
            or query["joins"][0].get("relationship", {}).get("constraint_name")
            != contract["relationship"]
            or query.get("group_by") != expected_groups
            or len(having) != 1
            or query.get("count_entity") != expected_count_entity
        ):
            raise QueryGenerationError(
                "Generated query does not implement the required business metric."
            )
        actual = having[0]
        try:
            values_match = Decimal(str(actual.get("value"))) == Decimal(
                str(expected_threshold)
            )
        except InvalidOperation:
            values_match = False
        if (
            actual.get("aggregate") != contract["aggregate"]
            or actual.get("table") != contract["source_table"]
            or actual.get("column") != contract["measure_column"]
            or actual.get("operator") != contract["operator"]
            or not values_match
        ):
            raise QueryGenerationError(
                "Generated HAVING threshold does not match the business definition or question."
            )
        if is_list_contract:
            expected_select_items = [
                {
                    "table": contract["entity_table"],
                    "column": contract["entity_key"],
                    "aggregate": None,
                    "distinct": False,
                    "alias": "shop_id",
                },
                {
                    "table": contract["entity_table"],
                    "column": contract["entity_label_column"],
                    "aggregate": None,
                    "distinct": False,
                    "alias": "shop_name",
                },
                {
                    "table": contract["source_table"],
                    "column": contract["measure_column"],
                    "aggregate": contract["aggregate"],
                    "distinct": False,
                    "alias": contract["measure_alias"],
                },
                {
                    "table": contract["source_table"],
                    "column": contract["count_column"],
                    "aggregate": "COUNT",
                    "distinct": True,
                    "alias": contract["count_alias"],
                },
            ]
            if (
                query.get("select_items") != expected_select_items
                or query.get("order_by") != [{
                    "column": contract["measure_alias"],
                    "direction": "DESC",
                }]
            ):
                raise QueryGenerationError(
                    "Generated listing does not return the required entity, average, and review count."
                )


def _columns_for_table(schema_context: Dict[str, Any], table: str) -> set[str]:
    return {
        column["column"]
        for column in schema_context.get(table, [])
        if isinstance(column, dict) and isinstance(column.get("column"), str)
    }


def _resolve_column(
    item: Dict[str, Any],
    schema_context: Dict[str, Any],
    query_tables: set[str],
    *,
    allow_star: bool = False,
) -> tuple[str, str]:
    column = item.get("column")
    if not isinstance(column, str) or not column:
        raise QueryGenerationError(f"Column reference must be a non-empty string. Got: {column!r}")
    table = item.get("table")
    if column == "*" and allow_star:
        if table is not None and (not isinstance(table, str) or table not in query_tables):
            raise QueryGenerationError(f"Column '*' references unknown query table {table!r}.")
        return table or next(iter(query_tables)), column
    if table is not None:
        if (
            not isinstance(table, str)
            or table not in query_tables
            or column not in _columns_for_table(schema_context, table)
        ):
            raise QueryGenerationError(f"Unknown column reference {table}.{column}.")
        return table, column

    owners = [
        candidate for candidate in query_tables
        if column in _columns_for_table(schema_context, candidate)
    ]
    if len(owners) != 1:
        raise QueryGenerationError(
            f"Column {column!r} must identify its table; matching tables are {owners}."
        )
    return owners[0], column


def _normalize_grouped_dimension_count(
    parsed: Dict[str, Any],
    question: str | None,
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
) -> Dict[str, Any]:
    """Use verified dimension metadata when a grouped FK was selected for an "each" count."""
    if not question or not re.search(
        r"\b(each|per|by)\s+(?:the\s+)?categor(?:y|ies)(?:_id)?\b",
        question,
        re.I,
    ):
        return parsed
    if parsed.get("answerable") is not True or parsed.get("joins") not in (None, []):
        return parsed

    table = parsed.get("table")
    select_items = parsed.get("select_items")
    group_by = parsed.get("group_by")
    if (
        not isinstance(table, str)
        or not isinstance(select_items, list)
        or not isinstance(group_by, list)
        or len(group_by) != 1
    ):
        return parsed

    grouped = group_by[0]
    if not isinstance(grouped, dict) or grouped.get("table") != table:
        return parsed
    foreign_key = grouped.get("column")
    if not isinstance(foreign_key, str) or not foreign_key.endswith("_id"):
        return parsed

    plain_dimensions = [
        item for item in select_items
        if isinstance(item, dict)
        and item.get("aggregate") is None
        and item.get("table") == table
        and item.get("column") == foreign_key
    ]
    count_items = [
        item for item in select_items
        if isinstance(item, dict) and item.get("aggregate") == "COUNT"
    ]
    if len(plain_dimensions) != 1 or len(count_items) != 1:
        return parsed

    matching_edges = [
        edge for edge in relationships or []
        if edge.get("table") == table
        and any(
            isinstance(pair, dict) and pair.get("column") == foreign_key
            for pair in edge.get("column_pairs", [])
        )
    ]
    if len(matching_edges) != 1:
        return parsed

    relationship = matching_edges[0]
    parent = relationship.get("referenced_table")
    pairs = relationship.get("column_pairs", [])
    pair = next(
        (
            pair for pair in pairs
            if isinstance(pair, dict) and pair.get("column") == foreign_key
        ),
        None,
    )
    if not isinstance(parent, str) or not isinstance(pair, dict):
        return parsed

    parent_key = pair.get("referenced_column")
    parent_columns = schema_context.get(parent, [])
    if not isinstance(parent_key, str) or not any(
        isinstance(column, dict)
        and column.get("column") == parent_key
        and column.get("key") == "PRI"
        for column in parent_columns
    ):
        return parsed

    label_columns = [
        column["column"]
        for column in parent_columns
        if isinstance(column, dict)
        and isinstance(column.get("column"), str)
        and column["column"].lower().endswith("name")
    ]
    if len(label_columns) != 1:
        return parsed

    entity_keys = [
        column["column"]
        for column in schema_context.get(table, [])
        if isinstance(column, dict) and column.get("key") == "PRI"
    ]
    if len(entity_keys) != 1:
        return parsed

    result = dict(parsed)
    result["table"] = parent
    result["joins"] = [{
        "table": table,
        "join_type": "LEFT",
        "relationship": {
            "constraint_name": relationship.get("constraint_name"),
            "table": relationship.get("table"),
            "referenced_table": parent,
        },
    }]
    dimension_alias = plain_dimensions[0].get("alias")
    alias_prefix = (
        dimension_alias[:-3]
        if isinstance(dimension_alias, str) and dimension_alias.endswith("_id")
        else foreign_key[:-3]
    )
    result["select_items"] = [
        {
            "table": parent,
            "column": label_columns[0],
            "aggregate": None,
            "alias": f"{alias_prefix}_name",
        },
        {
            **count_items[0],
            "table": table,
            "column": entity_keys[0],
            "distinct": True,
            "alias": count_items[0].get("alias") or "count",
        },
    ]
    result["group_by"] = [
        {"table": parent, "column": parent_key},
        {"table": parent, "column": label_columns[0]},
    ]
    result["count_entity"] = {"table": table, "column": entity_keys[0]}
    if not result.get("order_by"):
        result["order_by"] = [{
            "column": result["select_items"][1]["alias"],
            "direction": "DESC",
        }]
    return result


def _and_terms(expression: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Predicates that must ALL hold: the expression itself, or the members of
    (nested) AND groups. Anything under an OR is excluded on purpose."""
    if expression is None:
        return []
    if "boolean" not in expression:
        return [expression]
    if expression["boolean"] != "AND":
        return []
    return [
        term
        for condition in expression["conditions"]
        for term in _and_terms(condition)
    ]


# Keep these patterns tied to the grouped field; ambiguous intent preserves the filter.
_GROUPED_NULL_FILTER_INTENT_PATTERNS = (
    r"\b(?:exclud(?:e|es|ed|ing)|without|ignor(?:e|es|ed|ing)|skip(?:s|ped|ping)?)\b"
    r".{0,48}{column}",
    r"{column}.{0,48}\b(?:exclud(?:e|es|ed|ing)|without|ignor(?:e|es|ed|ing)|skip(?:s|ped|ping)?)\b",
    r"\bonly\s+those\s+with\s+(?:(?:a|an|the)\s+)?{column}",
    r"\bthat\s+have\s+(?:(?:a|an|the)\s+)?{column}",
    r"\bwith\s+(?:(?:a|an)\s+)?known\s+{column}",
    r"\b(?:non[-\s]?empty|not\s+empty|non[-\s]?null|not\s+null|missing|known)\b"
    r".{0,48}{column}",
    r"{column}.{0,48}\b(?:non[-\s]?empty|not\s+empty|non[-\s]?null|not\s+null|missing|known)\b",
    r"\bhas\s+(?:a|an)\s+{column}",
)
_EMPTY_VALUE_FILTER_OPERATORS = frozenset({"!=", "<>", "NOT IN"})
# Quoted labels are user data, not instructions about filter intent.
_QUOTED_TEXT_PATTERN = re.compile(
    r'"[^"\r\n]*"|“[^”\r\n]*”|‘[^’\r\n]*’|`[^`\r\n]*`'
    r"|(?<!\w)'[^'\r\n]*'(?!\w)"
)


def _grouped_column_reference_pattern(column: str) -> re.Pattern[str] | None:
    parts = re.findall(r"[a-z0-9]+", column.casefold())
    if not parts:
        return None
    variants = {" ".join(parts)}
    if parts[-1] in {"name", "id"} and len(parts) > 1:
        variants.add(" ".join(parts[:-1]))
    compact = "".join(parts)
    for suffix in ("name", "id"):
        if compact.endswith(suffix) and len(compact) > len(suffix) + 1:
            variants.add(compact[: -len(suffix)])
    if parts[0] in {"is"} and len(parts) > 1:
        variants.add(" ".join(parts[1:]))
    alternatives = []
    for variant in sorted(variants, key=len, reverse=True):
        variant_parts = variant.split()
        alternatives.append(
            r"(?<!\w)" + r"[\s_-]+".join(map(re.escape, variant_parts)) + r"(?!\w)"
        )
    return re.compile("|".join(alternatives), re.IGNORECASE)


def _explicit_grouped_null_filter_intent(
    question: str | None,
    column_names: set[str],
) -> bool:
    if not question or not question.strip():
        return False
    question = " ".join(_QUOTED_TEXT_PATTERN.sub(" ", question).split())
    for name in column_names:
        column_pattern = _grouped_column_reference_pattern(name)
        if column_pattern is None:
            continue
        for intent_template in _GROUPED_NULL_FILTER_INTENT_PATTERNS:
            pattern = re.compile(
                intent_template.replace("{column}", column_pattern.pattern),
                re.IGNORECASE,
            )
            if pattern.search(question):
                return True
    return False


def _is_grouped_null_or_empty_exclusion(
    predicate: Dict[str, Any],
) -> bool:
    operator = predicate.get("operator")
    if operator == "IS NOT NULL":
        return predicate.get("value") is None
    if operator not in _EMPTY_VALUE_FILTER_OPERATORS:
        return False
    value = predicate.get("value")
    if operator == "NOT IN":
        values = value if isinstance(value, list) else [value]
        return len(values) == 1 and values[0] == ""
    return isinstance(value, str) and not value.strip()


def _remove_unrequested_grouped_null_exclusions(
    where_expression: Dict[str, Any] | None,
    group_by: List[Dict[str, Any]],
    question: str | None,
    select_items: List[Dict[str, Any]] | None = None,
    joins: List[Dict[str, Any]] | None = None,
    schema_context: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    """Preserve NULL groups unless the question explicitly excludes their values."""
    if where_expression is None or not group_by:
        return where_expression

    grouped_columns = {
        (group.get("table"), group.get("column")): {
            str(group.get("column", "")),
            str(group.get("date_bucket", "")),
        }
        for group in group_by
        if isinstance(group.get("table"), str)
        and isinstance(group.get("column"), str)
    }
    for item in select_items or []:
        key = (item.get("table"), item.get("column"))
        alias = item.get("alias")
        if key in grouped_columns and isinstance(alias, str):
            grouped_columns[key].add(alias)

    protected_primary_keys: set[tuple[str, str]] = set()
    unquoted_question = (
        " ".join(_QUOTED_TEXT_PATTERN.sub(" ", question).split())
        if question
        else ""
    )
    if unquoted_question and schema_context:
        resolved_entity = _resolve_at_least_one_joined_entity(
            unquoted_question,
            joins or [],
            reject_unresolved=False,
        )
        if (
            resolved_entity is not None
            and resolved_entity[0].get("join_type") != "INNER"
        ):
            joined_table = resolved_entity[0].get("table")
            if isinstance(joined_table, str):
                protected_primary_keys.update(
                    (joined_table, str(column.get("column")))
                    for column in schema_context.get(joined_table, [])
                    if isinstance(column, dict)
                    and column.get("key") == "PRI"
                    and isinstance(column.get("column"), str)
                )
    if unquoted_question and _PLAN_ASSIGNMENT_PATTERN.search(unquoted_question):
        protected_primary_keys.add(("users", "plan_id"))

    removed: list[tuple[str, str]] = []

    def visit(node: Dict[str, Any]) -> Dict[str, Any] | None:
        if node.get("boolean") == "OR":
            return node
        if node.get("boolean") == "AND":
            children = []
            for child in node["conditions"]:
                updated = visit(child)
                if updated is not None:
                    children.append(updated)
            if not children:
                return None
            if len(children) == 1:
                return children[0]
            return {"boolean": "AND", "conditions": children}

        key = (node.get("table"), node.get("column"))
        names = grouped_columns.get(key)
        if (
            names is None
            or key in protected_primary_keys
            or not _is_grouped_null_or_empty_exclusion(node)
            or _explicit_grouped_null_filter_intent(question, names)
        ):
            return node

        removed.append((str(key[0]), str(key[1])))
        return None

    result = visit(where_expression)
    for table, column in removed:
        logger.info(
            "Removed unrequested grouped NULL/empty exclusion: "
            "table=%s column=%s reason=preserve_grouped_nulls",
            table,
            column,
        )
    return result


_DATE_BUCKETS = {"month", "quarter", "year"}
_DATE_TYPES = {"date", "datetime", "timestamp"}


def _schema_column_type(
    schema_context: Dict[str, Any], table: str, column: str
) -> str:
    return next(
        (
            str(entry.get("data_type", "")).lower()
            for entry in schema_context.get(table, [])
            if isinstance(entry, dict) and entry.get("column") == column
        ),
        "",
    )


def _enforce_explicit_year_scope(
    question: str | None,
    group_by: List[Dict[str, Any]],
    where_expression: Dict[str, Any] | None,
    schema_context: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not question or not re.search(r"\bmonths?\b", question, re.IGNORECASE):
        return where_expression
    years = re.findall(r"\b(?:19|20|21)\d{2}\b", question)
    distinct_years = sorted(set(years))
    if not distinct_years:
        return where_expression
    if len(distinct_years) > 1:
        raise QueryGenerationError(
            "Monthly grouping across multiple explicit years must keep each "
            "year distinct; use a year-and-month grouping."
        )

    year = int(distinct_years[0])
    month_groups = [
        group for group in group_by
        if group.get("date_bucket") == "month"
        or (
            group.get("date_bucket") is None
            and group.get("column", "").lower().endswith("_month")
        )
    ]
    if not month_groups:
        return where_expression

    date_column = None
    for group in month_groups:
        owner = group["table"]
        column = group["column"]
        if group.get("date_bucket") == "month":
            if _schema_column_type(schema_context, owner, column) in _DATE_TYPES:
                date_column = (owner, column)
                break
            raise QueryGenerationError(
                "A month date bucket must use a verified date or timestamp column."
            )
        companion_candidates = []
        if column.lower().endswith("_month"):
            prefix = column[:-len("_month")]
            companion_candidates = [f"{prefix}_date", f"{prefix}_at"]
        date_column = next(
            (
                (owner, candidate)
                for candidate in companion_candidates
                if _schema_column_type(schema_context, owner, candidate) in _DATE_TYPES
            ),
            None,
        )
        if date_column is None:
            date_candidates = [
                entry.get("column")
                for entry in schema_context.get(owner, [])
                if isinstance(entry, dict)
                and _schema_column_type(
                    schema_context, owner, str(entry.get("column", ""))
                ) in _DATE_TYPES
            ]
            if len(date_candidates) == 1:
                date_column = (owner, date_candidates[0])
        if date_column:
            break
    if date_column is None:
        raise QueryGenerationError(
            "A month grouped within an explicit year needs a verified companion "
            "date column so the requested year can be filtered."
        )

    owner, column = date_column
    lower_bound = f"{year:04d}-01-01"
    upper_bound = f"{year + 1:04d}-01-01"
    conditions = [
        {"table": owner, "column": column, "operator": ">=", "value": lower_bound},
        {"table": owner, "column": column, "operator": "<", "value": upper_bound},
    ]
    existing_terms = _and_terms(where_expression)
    date_terms = [
        term for term in _offer_filter_leaves(where_expression)
        if term.get("table") == owner and term.get("column") == column
    ]
    if date_terms:
        correctly_scoped_terms = [
            term for term in date_terms
            if (
                term.get("operator") == ">="
                and term.get("value") == lower_bound
            ) or (
                term.get("operator") == "<"
                and term.get("value") == upper_bound
            )
        ]
        has_lower = any(
            term.get("operator") == ">=" and term.get("value") == lower_bound
            for term in correctly_scoped_terms
        )
        has_upper = any(
            term.get("operator") == "<" and term.get("value") == upper_bound
            for term in correctly_scoped_terms
        )
        if (
            has_lower
            and has_upper
            and len(correctly_scoped_terms) == len(date_terms)
            and all(term in existing_terms for term in correctly_scoped_terms)
        ):
            return where_expression
        raise QueryGenerationError(
            f"Monthly results for {year} must use the complete half-open date range "
            f"[{lower_bound}, {upper_bound})."
        )

    if where_expression is None:
        return {"boolean": "AND", "conditions": conditions}
    return {
        "boolean": "AND",
        "conditions": [where_expression, *conditions],
    }


def _normalize_booking_month_semantics(
    question: str | None,
    query_tables: set[str],
    select_items: List[Dict[str, Any]],
    group_by: List[Dict[str, Any]],
    where_expression: Dict[str, Any] | None,
    schema_context: Dict[str, Any],
) -> Dict[str, Any] | None:
    if (
        not question
        or "bookings" not in query_tables
        or not re.search(r"\bmonths?\b|\bmonthly\b", question, re.IGNORECASE)
        or re.search(
            r"\b(created|creation|recorded|submitted|placed)\b",
            question,
            re.IGNORECASE,
        )
        or _schema_column_type(schema_context, "bookings", "time_slot_date")
        not in _DATE_TYPES
    ):
        return where_expression

    month_groups = [
        group
        for group in group_by
        if group.get("table") == "bookings"
        and (
            group.get("date_bucket") == "month"
            or (
                group.get("date_bucket") is None
                and (
                    group.get("column", "").lower().endswith("_month")
                    or _schema_column_type(
                        schema_context, "bookings", group.get("column", "")
                    ) in _DATE_TYPES
                )
            )
        )
    ]
    if not month_groups:
        return where_expression

    month_column_exists = any(
        isinstance(entry, dict) and entry.get("column") == "time_slot_month"
        for entry in schema_context.get("bookings", [])
    )
    original_groups = [
        (group["column"], group.get("date_bucket")) for group in month_groups
    ]
    for group in month_groups:
        group["column"] = "time_slot_month" if month_column_exists else "time_slot_date"
        if month_column_exists:
            group.pop("date_bucket", None)
        else:
            group["date_bucket"] = "month"

    for item in select_items:
        if (
            item.get("table") == "bookings"
            and item.get("aggregate") is None
            and (item.get("column"), item.get("date_bucket")) in original_groups
        ):
            item["column"] = (
                "time_slot_month" if month_column_exists else "time_slot_date"
            )
            if month_column_exists:
                item.pop("date_bucket", None)
            else:
                item["date_bucket"] = "month"

    years = re.findall(r"\b(?:19|20|21)\d{2}\b", question)
    if len(set(years)) != 1:
        return where_expression
    year = int(years[0])
    old_date_columns = {
        column
        for column, date_bucket in original_groups
        if _schema_column_type(schema_context, "bookings", column) in _DATE_TYPES
    }
    if not old_date_columns:
        return where_expression

    lower_bound = f"{year:04d}-01-01"
    upper_bound = f"{year + 1:04d}-01-01"

    def remove_old_year_bounds(expression: Dict[str, Any] | None):
        if expression is None:
            return None
        if "boolean" not in expression:
            if (
                expression.get("table") == "bookings"
                and expression.get("column") in old_date_columns
                and (
                    expression.get("operator"),
                    expression.get("value"),
                ) in {(">=", lower_bound), ("<", upper_bound)}
            ):
                return None
            return expression
        if expression.get("boolean") != "AND":
            return expression
        conditions = [
            remaining
            for condition in expression.get("conditions", [])
            if (remaining := remove_old_year_bounds(condition)) is not None
        ]
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"boolean": "AND", "conditions": conditions}

    return remove_old_year_bounds(where_expression)


def _offer_filter_leaves(node: Any) -> List[Dict[str, Any]]:
    """All single conditions inside a (possibly nested) where expression."""
    if not isinstance(node, dict):
        return []
    children = node.get("conditions")
    if isinstance(children, list):
        leaves: List[Dict[str, Any]] = []
        for child in children:
            leaves.extend(_offer_filter_leaves(child))
        return leaves
    return [node]


def _reject_offer_heading_as_shop_type(
    expression: Dict[str, Any] | None,
    question: str | None,
) -> None:
    """
    An offer heading that mentions a product does not prove the shop sells it.
    For shop discovery questions, reject plans that filter on offer_heading
    (unless the user asked about offers, deals, discounts, coupons).
    """
    if not question or not expression:
        return
    question_lower = question.casefold()
    discovery_words = re.search(
        r"\b(?:shops?|places?|restaurants?|mechanics?|taxi|taxis|cabs?|"
        r"where\s+can\s+i\s+buy)\b",
        question_lower,
    )
    explicit_offer_request = re.search(
        r"\b(?:offers?|deals?|discounts?|coupons?|promotions?)\b",
        question_lower,
    )
    if not discovery_words or explicit_offer_request:
        return
    if any(
        leaf.get("table") == "offer" and leaf.get("column") == "offer_heading"
        for leaf in _offer_filter_leaves(expression)
    ):
        raise QueryGenerationError(
            "An offer title mentioning a product or service does not establish "
            "that the shop provides it. Search shop identity or verified "
            "category fields for shop discovery.",
            rule_id="offer_heading_not_shop_type",
        )


def _question_requires_primary_address(question: str | None) -> bool:
    if not question or re.search(
        r"\b(?:all|every)\s+(?:of\s+the\s+)?(?:shop\s+)?addresses\b",
        question,
        re.IGNORECASE,
    ):
        return False
    return bool(re.search(
        r"\b(default|main|primary)\s+(?:address|location)\b"
        r"|\b(?:full\s+address|address\s+details|address\s+of|store\s+address|"
        r"location\s+details|location\s+of|nearby\s+landmark|landmark)\b"
        r"|\b(?:show|list|display|get|give)\b.{0,60}\b(?:address|landmark)\b",
        question,
        re.IGNORECASE,
    ))


_ALTERNATE_ADDRESS_SCOPE_PATTERN = re.compile(
    r"\b(?:(?:all|every)\s+(?:of\s+the\s+)?(?:shop\s+)?address(?:es)?"
    r"|any\s+(?:shop\s+)?address(?:es)?"
    r"|delivery\s+address(?:es)?)\b",
    re.IGNORECASE,
)


def _question_requests_alternate_address_scope(question: str | None) -> bool:
    """Recognize explicit requests that intentionally avoid the default address."""
    return bool(
        question
        and _ALTERNATE_ADDRESS_SCOPE_PATTERN.search(question)
    )


# These verified shop_address columns represent the shop's physical location.
_SHOP_LOCATION_COLUMNS = frozenset(
    {"city", "arearoadname", "nearbylandmark", "pincode"}
)


def _enforce_default_address_filter(
    where_expression: Dict[str, Any] | None,
    query_tables: set[str],
    schema_context: Dict[str, Any],
    question: str | None = None,
    select_items: List[Dict[str, Any]] | None = None,
    joins: List[Dict[str, Any]] | None = None,
    group_by: List[Dict[str, Any]] | None = None,
) -> None:
    """Use a representative shop address except for explicit alternate scopes."""
    if "shop_address" not in query_tables:
        return
    address_filters = list(_and_terms(where_expression))
    for join in joins or []:
        if not isinstance(join, dict) or join.get("table") != "shop_address":
            continue
        conditions = join.get("conditions")
        if isinstance(conditions, dict):
            address_filters.extend(_and_terms(conditions))
        elif isinstance(conditions, list):
            for condition in conditions:
                if isinstance(condition, dict):
                    address_filters.extend(_and_terms(condition))

    counts_distinct_shops = any(
        item.get("table") == "shop_details"
        and item.get("aggregate") == "COUNT"
        and item.get("distinct") is True
        and _is_single_primary_key(
            schema_context, "shop_details", item.get("column")
        )
        for item in (select_items or [])
    )
    asks_primary_address = _question_requires_primary_address(question)
    if _question_requests_alternate_address_scope(question):
        return
    location_columns = (
        _SHOP_LOCATION_COLUMNS
        & set(_columns_for_table(schema_context, "shop_address"))
    )
    location_scoped_shop_query = (
        "shop_details" in query_tables
        and (
            any(
                leaf.get("table") == "shop_address"
                and leaf.get("column") in location_columns
                for leaf in _expression_leaves(where_expression)
            )
            or any(
                item.get("table") == "shop_address"
                and item.get("column") in location_columns
                for item in (group_by or [])
            )
        )
    )
    if (
        counts_distinct_shops
        and not asks_primary_address
        and not location_scoped_shop_query
    ):
        if any(
            term.get("table") == "shop_address"
            and term.get("column") == "is_default"
            for term in address_filters
        ):
            raise QueryGenerationError(
                "A distinct shop count without a location scope must include "
                "any matching address; do not add is_default unless requested.",
                rule_id="shop_count_address_scope",
            )
        return
    if "is_default" not in _columns_for_table(schema_context, "shop_address"):
        raise QueryGenerationError(
            "shop_address has no is_default column, so one address per shop cannot be selected safely.",
            rule_id="address_default_column_missing",
        )
    if not any(
        term.get("table") == "shop_address"
        and term.get("column") == "is_default"
        and term.get("operator") == "="
        and str(term.get("value")) == "1"
        and not isinstance(term.get("value"), bool)
        for term in address_filters
    ):
        raise QueryGenerationError(
            "Using shop_address requires shop_address.is_default = 1 unless the "
            "user explicitly requests an alternate address scope.",
            rule_id="address_requires_default",
        )


_NAME_FILTER_LABELS = {
    "subcategories.subcategoryname": "subcategory",
    "categories.categoriesname": "category",
}


def _reject_ambiguous_name_filters(
    where_expression: Dict[str, Any] | None,
) -> None:
    """A category/subcategory name used by several rows would silently merge
    unrelated groups, so ask the user to clarify instead."""
    ambiguous = get_ambiguous_names()
    if not ambiguous:
        return
    for leaf in _expression_leaves(where_expression):
        key = f"{leaf.get('table')}.{leaf.get('column')}"
        label = _NAME_FILTER_LABELS.get(key)
        repeated = ambiguous.get(key)
        if label is None or not repeated:
            continue
        if leaf.get("operator") not in {"=", "IN", "LIKE"}:
            continue
        value = leaf.get("value")
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip("% ").casefold() in repeated:
                raise UnanswerableQuestionError(
                    f"The {label} name '{item.strip('% ')}' is used by more than "
                    f"one {label}. Please say which one you mean "
                    f"(for example, by naming its parent category)."
                )

_DISCOUNT_PATTERN = re.compile(r"\bdiscount(?:s|ed)?\b", re.IGNORECASE)


def _validate_discount_source(
    query: Dict[str, Any],
    question_text: str,
    schema_context: Dict[str, Any],
) -> None:
    """A discount question must not be answered from another table's discount
    column unless the user named that table. The official discount is
    offer.actual_price - offer.offer_price (see business_rules.json)."""
    if not _DISCOUNT_PATTERN.search(question_text):
        return
    tables = {query.get("table")}
    tables.update(
        join.get("table")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    )
    if "offer" in tables:
        return
    discount_tables = [
        table
        for table in tables
        if isinstance(table, str)
        and any(
            "discount" in column.casefold()
            for column in _columns_for_table(schema_context, table)
        )
    ]
    if not discount_tables:
        return
    words = set(re.findall(r"[a-z0-9_]+", question_text.casefold()))
    if any(words & _table_name_forms(table) for table in discount_tables):
        return
    raise UnanswerableQuestionError(
        "I can't answer this reliably: the discount you asked about is not "
        "stored as a single value I can use safely. Please say which kind of "
        "discount you mean."
    )


_DISCOUNT_AMOUNT_PATTERN = re.compile(r"\bdiscounts?\b", re.IGNORECASE)
_OFFER_PRICE_COLUMNS = {"actual_price", "offer_price"}


def _iter_conditions(expression):
    """Yield every leaf condition from a nested where/having expression."""
    if not isinstance(expression, dict):
        return
    nested = expression.get("conditions")
    if isinstance(nested, list):
        for item in nested:
            yield from _iter_conditions(item)
    else:
        yield expression


def _validate_discount_not_price_substitute(
    query: Dict[str, Any],
    question_text: str,
) -> None:
    """Prevent quantified savings questions from substituting a single price."""
    if not (
        _DISCOUNT_AMOUNT_PATTERN.search(question_text)
        or _UNSUPPORTED_SAVINGS_PATTERN.search(question_text)
    ):
        return
    checked = []
    for key in ("where_expression", "having_expression"):
        checked.extend(_iter_conditions(query.get(key)))
    if any(item.get("metric") == "savings" for item in checked):
        return
    if _UNSUPPORTED_SAVINGS_PATTERN.search(question_text):
        raise UnanswerableQuestionError(
            "The requested savings threshold must filter on the validated savings metric."
        )
    checked.extend(
        item
        for item in query.get("select_items", [])
        if isinstance(item, dict) and item.get("aggregate")
    )
    for item in checked:
        if (
            item.get("table") == "offer"
            and item.get("column") in _OFFER_PRICE_COLUMNS
        ):
            raise UnanswerableQuestionError(
                "I can't answer this reliably: the requested savings need the "
                "difference between the actual price and the offer price, "
                "which I can't calculate safely."
            )


def _reject_distant_anti_join(
    where_expression: Dict[str, Any] | None,
    base_table: str,
    normalized_joins: List[Dict[str, Any]],
) -> None:
    """'<table>.x IS NULL' on a LEFT-joined table only means 'no match' when that
    table joins directly to the base table. Through an intermediate table it
    returns entities with at least one non-matching row."""
    for term in _and_terms(where_expression):
        if term.get("operator") != "IS NULL":
            continue
        owner = term.get("table")
        join = next((j for j in normalized_joins if j["table"] == owner), None)
        if join is None or join["join_type"] != "LEFT":
            continue
        relationship = join["relationship"]
        parent = (
            relationship["referenced_table"]
            if relationship["table"] == owner
            else relationship["table"]
        )
        if parent != base_table:
            raise QueryGenerationError(
                "IS NULL on a table joined through an intermediate table does not "
                "mean 'has none'. Group by the entity and use HAVING COUNT(...) = 0.",
                rule_id="anti_join_not_adjacent",
            )


def _apply_name_search_filters(
    normalized_joins: List[Dict[str, Any]],
    where_expression: Dict[str, Any] | None,
    search: Dict[str, str] | None,
    catalog_filters: list[Dict[str, Any]] | None,
    query_tables: set[str],
) -> tuple[Dict[str, Any] | None, Dict[str, str] | None]:
    terms = list(dict.fromkeys(
        normalize_shop_name_term(item["term"])
        for item in catalog_filters or []
        if item.get("kind") == "name_search"
        and isinstance(item.get("term"), str)
    ))
    if not terms:
        return where_expression, search

    if "shop_details" not in query_tables:
        raise QueryGenerationError(
            "A shop-name search requires shop_details in the query plan."
        )

    term_keys = {term.replace("%", "").strip().casefold() for term in terms}

    def matches_name_search(value: Any) -> bool:
        return (
            isinstance(value, str)
            and value.replace("%", "").strip().casefold() in term_keys
        )

    def strip_expression(expression: Any) -> Dict[str, Any] | None:
        if not isinstance(expression, dict):
            return None
        if expression.get("boolean") in ALLOWED_BOOLEAN_OPERATORS:
            children = [
                cleaned
                for child in expression.get("conditions", [])
                if (cleaned := strip_expression(child)) is not None
            ]
            if not children:
                return None
            if len(children) == 1:
                return children[0]
            return {**expression, "conditions": children}

        value = expression.get("value")
        if isinstance(value, list):
            retained_values = [
                item for item in value if not matches_name_search(item)
            ]
            if not retained_values:
                return None
            if len(retained_values) != len(value):
                return {**expression, "value": retained_values}
        elif matches_name_search(value) or matches_name_search(
            expression.get("term")
        ):
            return None
        return expression

    for join in normalized_joins:
        conditions = join.get("conditions")
        if isinstance(conditions, list):
            join["conditions"] = [
                cleaned
                for condition in conditions
                if (cleaned := strip_expression(condition)) is not None
            ]

    where_expression = strip_expression(where_expression)
    if isinstance(search, dict) and matches_name_search(search.get("term")):
        search = None

    conditions = [
        {
            "table": "shop_details",
            "column": "name",
            "operator": "=",
            "value": term,
        }
        for term in terms
        if term
    ]
    if not conditions:
        raise QueryGenerationError("A shop-name search requires a non-empty term.")
    if where_expression is not None:
        conditions.insert(0, where_expression)
    if len(conditions) == 1:
        return conditions[0], search
    return {"boolean": "AND", "conditions": conditions}, search


def _strip_catalog_filter_predicates(
    expression: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    if not isinstance(expression, dict):
        return expression
    if expression.get("boolean") not in {"AND", "OR"}:
        return None if is_catalog_filter_predicate(expression) else expression
    remaining = [
        child
        for item in expression.get("conditions", [])
        if (child := _strip_catalog_filter_predicates(item)) is not None
    ]
    if not remaining:
        return None
    if len(remaining) == 1:
        return remaining[0]
    return {**expression, "conditions": remaining}


def _catalog_predicates(expression: Any) -> list[Dict[str, Any]]:
    if not isinstance(expression, dict):
        return []
    if expression.get("boolean") in {"AND", "OR"}:
        return [
            predicate
            for child in expression.get("conditions", [])
            for predicate in _catalog_predicates(child)
        ]
    return [expression] if is_catalog_filter_predicate(expression) else []


def _apply_resolved_catalog_filters(
    where_expression: Dict[str, Any] | None,
    joins: List[Dict[str, Any]],
    catalog_filters: list[Dict[str, Any]] | None,
    query_tables: set[str],
) -> Dict[str, Any] | None:
    if not catalog_filters:
        return where_expression
    try:
        catalog_expression = build_catalog_filter_expression(catalog_filters)
    except ValueError as exc:
        raise QueryGenerationError(str(exc)) from exc
    if catalog_expression is None:
        return where_expression
    expected_catalog_filters = [
        item for item in catalog_filters
        if item.get("kind", "catalog") == "catalog"
    ]
    existing_catalog_predicates = _catalog_predicates(where_expression)
    for join in joins:
        for condition in join.get("conditions") or []:
            existing_catalog_predicates.extend(_catalog_predicates(condition))
    if any(
        not catalog_filter_matches_expected(predicate, expected_catalog_filters)
        for predicate in existing_catalog_predicates
    ):
        return where_expression

    required_tables = {
        leaf["table"]
        for leaf in _expression_leaves(catalog_expression)
    }
    missing_tables = required_tables - query_tables
    if missing_tables:
        raise QueryGenerationError(
            "Add a verified join to the required catalog table(s) "
            f"{sorted(missing_tables)} before applying the resolved shop type."
        )

    normalized_where = _strip_catalog_filter_predicates(where_expression)
    for join in joins:
        conditions = join.get("conditions")
        if isinstance(conditions, list):
            join["conditions"] = [
                child for condition in conditions
                if (child := _strip_catalog_filter_predicates(condition)) is not None
            ]

    if normalized_where is None:
        return catalog_expression
    return {
        "boolean": "AND",
        "conditions": [normalized_where, catalog_expression],
    }


def _reject_unused_left_join(
    normalized_joins: List[Dict[str, Any]],
    select_items: List[Dict[str, Any]],
    where_expression: Dict[str, Any] | None,
    group_by: List[Dict[str, Any]],
    having_expression: Dict[str, Any] | None,
    order_by: List[Dict[str, Any]],
    count_entity: Dict[str, Any] | None,
) -> None:
    """A LEFT-joined table that no clause references changes nothing. It means
    a condition such as 'has no X' was dropped, so the answer would be wrong."""
    used = {item.get("table") for item in select_items}
    used.update(leaf.get("table") for leaf in _expression_leaves(where_expression))
    used.update(leaf.get("table") for leaf in _expression_leaves(having_expression))
    used.update(item.get("table") for item in group_by)
    used.update(item.get("table") for item in order_by)
    if count_entity:
        used.add(count_entity.get("table"))
    for join in normalized_joins:
        if join.get("conditions"):
            used.add(join["table"])
       # Tables that another join hangs off are bridges, not unused joins.
    bridges = set()
    for join in normalized_joins:
        relationship = join["relationship"]
        other = (
            relationship["referenced_table"]
            if relationship["table"] == join["table"]
            else relationship["table"]
        )
        bridges.add(other)
    for join in normalized_joins:
        if (
            join["join_type"] == "LEFT"
            and join["table"] not in used
            and join["table"] not in bridges
        ):
            raise QueryGenerationError(
                f"The LEFT JOIN to '{join['table']}' is never used by any "
                "condition, so part of the question was dropped. For 'has no X', "
                "filter X's primary key IS NULL.",
                rule_id="unused_left_join",
            )
        
      
def _log_rejected_count_plan(
    rule_id: str,
    attempt_number: int | None,
    base_table: str,
    owner: str,
    column: str,
    normalized_joins: List[Dict[str, Any]],
) -> None:
    """Structure-only diagnostics for joined COUNT rejections. Never raises."""
    try:
        join_evidence = [
            {
                "joined_table": join["table"],
                "relationship_table": join["relationship"]["table"],
                "referenced_table": join["relationship"]["referenced_table"],
                "cardinality": join["relationship"].get("cardinality"),
            }
            for join in normalized_joins
        ]
        logger.warning(
            "Node 6 rejected joined COUNT plan: rule_id=%s attempt=%r "
            "base_table=%r count_owner_table=%r count_column=%r joins=%r",
            rule_id, attempt_number, base_table, owner, column, join_evidence,
        )
    except Exception:
        pass  # diagnostics must never change behaviour


def _validate_llm_output(
    parsed: Dict[str, Any],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None = None,
    question: str | None = None,
    matched_business_rules: List[Dict[str, Any]] | None = None,
    attempt_number: int | None = None,
    reject_offer_heading_as_shop_type: bool = False,
    original_question: str | None = None,
    catalog_filters: list[Dict[str, Any]] | None = None,
    resolved_temporal_constraints: list[Dict[str, Any]] | None = None,
    current_state_filters: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Validate and normalize the model's structured query against live schema."""
    if not isinstance(parsed, dict):
        raise QueryGenerationError("LLM response must be a JSON object.")
    _reject_model_supplied_subquery(parsed)
    unsupported_fields = set(parsed) - set(QUERY_PLAN_FIELDS)
    if unsupported_fields:
        raise QueryGenerationError(
            "Unsupported query-plan field(s): "
            f"{sorted(unsupported_fields)}. Use only fields in the documented plan contract."
        )
    parsed = _normalize_grouped_dimension_count(
        parsed, question, schema_context, relationships
    )
    required_keys = {"answerable", "reasoning"}
    missing = required_keys - parsed.keys()
    if missing:
        raise QueryGenerationError(f"LLM response missing required keys: {missing}. Got: {parsed}")

    if not isinstance(parsed["answerable"], bool):
        raise QueryGenerationError(f"'answerable' must be a boolean, got: {parsed['answerable']!r}")

    if not parsed["answerable"]:
        return parsed

    table = parsed.get("table")
    if not isinstance(table, str) or not table or table not in schema_context:
        raise QueryGenerationError(
            f"LLM chose table '{table}' which is not in the known schema: {list(schema_context.keys())}. "
            f"Refusing to trust a table name we have no schema evidence for."
        )

    normalized_search = None
    search = parsed.get("search")
    if search is not None:
        if not isinstance(search, dict) or set(search) != {"term", "scope"}:
            raise QueryGenerationError(
                "The search operation must contain exactly 'term' and 'scope'; "
                "it cannot contain identifiers or SQL."
            )
        term = search.get("term")
        scope = search.get("scope")
        if not isinstance(term, str) or not term.strip():
            raise QueryGenerationError("Search term must be a non-empty string.")
        term = term.strip()
        if len(term) > MAX_SEARCH_TERM_LENGTH:
            raise QueryGenerationError(
                f"Search terms may not exceed {MAX_SEARCH_TERM_LENGTH} characters."
            )
        if not isinstance(scope, str) or scope not in SEARCHABLE_COLUMNS:
            raise QueryGenerationError(
                f"Unsupported search scope {scope!r}."
            )
        try:
            validate_searchable_columns(schema_context, relationships or [])
        except SearchConfigurationError as exc:
            raise QueryGenerationError(
                f"Search configuration is invalid for the current schema: {exc}"
            ) from exc
        if parsed.get("count_groups") is True or parsed.get(
            "include_empty_groups"
        ) is True:
            raise QueryGenerationError(
                "The typed search operation cannot be combined with count_groups "
                "or include_empty_groups."
            )
        normalized_search = {"term": term, "scope": scope}

    joins = parsed.get("joins", [])
    if not isinstance(joins, list):
        raise QueryGenerationError("'joins' must be a list.")
    if len(joins) > MAX_QUERY_TABLES - 1:
        raise QueryGenerationError(
            f"Queries may use at most {MAX_QUERY_TABLES} tables, including the base table."
        )

    relationship_map = {
        (
            edge.get("constraint_name"),
            edge.get("table"),
            edge.get("referenced_table"),
        ): edge
        for edge in relationships or []
    }
    query_tables = {table}
    count_groups = parsed.get("count_groups", False)
    if not isinstance(count_groups, bool):
        raise QueryGenerationError("'count_groups' must be a boolean.")
    projection_distinct = parsed.get("distinct", False)
    if not isinstance(projection_distinct, bool):
        raise QueryGenerationError("'distinct' must be a boolean.")
    if projection_distinct and (
        parsed.get("count_groups") is True
        or parsed.get("group_by")
        or parsed.get("having_expression") is not None
    ):
        raise QueryGenerationError(
            "Projection DISTINCT is supported only for ungrouped row projections."
        )
    normalized_joins = []
    fanout_candidates = []
    for join in joins:
        if not isinstance(join, dict):
            raise QueryGenerationError("Each join must be a JSON object.")
        new_table = join.get("table")
        join_type = join.get("join_type")
        relationship = join.get("relationship")
        if not isinstance(new_table, str) or new_table not in schema_context or new_table in query_tables:
            raise QueryGenerationError(
                f"Join introduces invalid or repeated table {new_table!r}.",
                rule_id="repeated_join_table",
            )
        if not isinstance(join_type, str) or join_type not in ALLOWED_JOIN_TYPES:
            raise QueryGenerationError(f"Unsupported join type {join_type!r}.")
        if not isinstance(relationship, dict):
            raise QueryGenerationError("Each join must identify a verified relationship.")
        if not all(
            isinstance(relationship.get(key), str)
            for key in ("constraint_name", "table", "referenced_table")
        ):
            raise QueryGenerationError("Join relationship identifiers must be strings.")
        relation_key = (
            relationship.get("constraint_name"),
            relationship.get("table"),
            relationship.get("referenced_table"),
        )
        verified = relationship_map.get(relation_key)
        if verified is None:
            raise QueryGenerationError(
                f"Join relationship {relation_key!r} is not in the verified schema catalog."
            )
        endpoints = {verified["table"], verified["referenced_table"]}
        existing_endpoints = endpoints & query_tables
        if new_table not in endpoints or len(existing_endpoints) != 1:
            raise QueryGenerationError(
                f"Join to {new_table!r} must connect it to exactly one table already in the query."
            )
        query_tables.add(new_table)
        raw_conditions = join.get("conditions", [])
        normalized_conditions = []
        if raw_conditions:
            if not isinstance(raw_conditions, list):
                raise QueryGenerationError(f"Join 'conditions' for {new_table!r} must be a list.")
            target_cols = _columns_for_table(schema_context, new_table)
            for cond in raw_conditions:
                if not isinstance(cond, dict):
                    raise QueryGenerationError("Each join condition must be a JSON object.")
                cond_table = cond.get("table")
                if cond_table is not None and cond_table != new_table:
                    raise QueryGenerationError(
                        f"Join condition table '{cond_table}' must match joined table '{new_table}'."
                    )
                col_name = cond.get("column")
                if not col_name or col_name not in target_cols:
                    raise QueryGenerationError(
                        f"Join condition column '{col_name}' does not belong to table '{new_table}'."
                    )
                op = cond.get("operator")
                if op not in ALLOWED_OPERATORS:
                    raise QueryGenerationError(f"Disallowed join condition operator: {op!r}.")
                val = cond.get("value")
                col_type = _schema_column_type(schema_context, new_table, col_name)
                coerced_val = (
                    _coerce_filter_value(val, col_type, table=new_table, column=col_name)
                    if op not in {"IS NULL", "IS NOT NULL"}
                    else None
                )
                cond_dict = {"table": new_table, "column": col_name, "operator": op}
                if op not in {"IS NULL", "IS NOT NULL"}:
                    cond_dict["value"] = coerced_val
                normalized_conditions.append(cond_dict)

        join_item = {
            "table": new_table,
            "join_type": join_type,
            "relationship": {
                "constraint_name": verified.get("constraint_name", ""),
                "table": verified["table"],
                "referenced_table": verified["referenced_table"],
                "cardinality": verified.get("cardinality", "many_to_one"),
                "verification": verified.get("verification", "reviewed_manual_query"),
                "single_row_when": verified.get("single_row_when"),
            },
        }
        if normalized_conditions:
            join_item["conditions"] = normalized_conditions
        normalized_joins.append(join_item)

    available_parent_tables = {
        table,
        *(join["table"] for join in normalized_joins),
    }
    normalized_exclusions, isolated_child_aggregates = (
        _normalize_isolated_query_parts(
            parsed,
            base_table=table,
            available_parent_tables=available_parent_tables,
            schema_context=schema_context,
            relationships=relationships,
            relationship_map=relationship_map,
        )
    )
    isolated_metrics = _extract_plan_metrics({
        "isolated_child_aggregates": isolated_child_aggregates,
    })

    select_items = parsed.get("select_items")
    if not isinstance(select_items, list) or not select_items:
        raise QueryGenerationError(f"'select_items' must be a non-empty list. Got: {select_items!r}")
    select_items = _expand_plain_wildcard_select_items(
        select_items,
        query_tables,
        schema_context,
    )

    normalized_select_items = []
    aliases = set()
    for item in select_items:
        if not isinstance(item, dict):
            raise QueryGenerationError("Each select_item must be a JSON object.")
        metric_name = item.get("metric")
        isolated_reference = (
            metric_name
            if isinstance(metric_name, str)
            and metric_name.casefold() in {
                alias.casefold() for alias in isolated_metrics
            }
            else (
                item.get("column")
                if isinstance(item.get("column"), str)
                and item["column"].casefold() in {
                    alias.casefold() for alias in isolated_metrics
                }
                else None
            )
        )
        if isolated_reference is not None:
            isolated_alias = next(
                alias
                for alias in isolated_metrics
                if alias.casefold() == isolated_reference.casefold()
            )
            if set(item) - {
                "metric", "column", "table", "aggregate", "distinct", "alias",
            }:
                raise QueryGenerationError(
                    "An isolated metric selection may reference only its "
                    "declared alias; filters and expressions are not supported."
                )
            selected_alias = item.get("alias", isolated_alias)
            metric_table = isolated_metrics[isolated_alias]["table"]
            if (
                item.get("aggregate") is not None
                or item.get("distinct", False)
                or not isinstance(selected_alias, str)
                or selected_alias.casefold() != isolated_alias.casefold()
                or (
                    item.get("table") is not None
                    and item.get("table") != metric_table
                )
            ):
                raise QueryGenerationError(
                    f"Select isolated metric {isolated_alias!r} using its declared "
                    "alias without another aggregate or a renamed output."
                )
            continue
        metric_condition = None
        aggregate_condition = None
        period_condition = None
        period_comparison = None
        if metric_name is not None:
            if set(item) - {
                "metric", "condition", "aggregate", "distinct", "alias",
                "round_digits",
            }:
                raise QueryGenerationError(
                    "Calculated-metric select items may not contain model-supplied "
                    "columns or expressions."
                )
            if metric_name == "share_of_rows":
                if "period" in item:
                    raise QueryGenerationError(
                        "Conditional periods are supported only for column aggregates."
                    )
                try:
                    metric_condition = validate_share_condition(
                        item.get("condition"), schema_context, query_tables
                    )
                except ValueError as exc:
                    raise QueryGenerationError(str(exc)) from exc
                owner, column = metric_condition["table"], metric_name
            else:
                if "condition" in item:
                    raise QueryGenerationError(
                        "Only share_of_rows accepts a metric condition."
                    )
                try:
                    metric_definition = validate_metric(
                        metric_name, schema_context, query_tables
                    )
                except ValueError as exc:
                    raise QueryGenerationError(str(exc)) from exc
                owner, column = metric_definition.table, metric_name
        else:
            if "period" in item and "period_comparison" in item:
                raise QueryGenerationError(
                    "A select item cannot contain both period and period_comparison."
                )
            if "period" in item:
                try:
                    period_condition = normalize_period(
                        item["period"], schema_context, query_tables
                    )
                except ValueError as exc:
                    raise QueryGenerationError(str(exc)) from exc
            if "period_comparison" in item:
                try:
                    period_comparison = normalize_period_comparison(
                        item["period_comparison"], schema_context, query_tables
                    )
                except ValueError as exc:
                    raise QueryGenerationError(str(exc)) from exc
            owner, column = _resolve_column(
                item, schema_context, query_tables, allow_star=True
            )
        aggregate = item.get("aggregate")
        if aggregate is not None and (
            not isinstance(aggregate, str) or aggregate not in ALLOWED_AGGREGATES
        ):
            raise QueryGenerationError(
                f"select_items uses disallowed aggregate {aggregate!r}. Allowed: {ALLOWED_AGGREGATES}"
            )
        if metric_name is None and "condition" in item:
            if aggregate != "COUNT":
                raise QueryGenerationError(
                    "A select-item condition is supported only for COUNT aggregates."
                )
            if period_condition is not None or period_comparison is not None:
                raise QueryGenerationError(
                    "A conditional COUNT cannot combine a row condition with a period."
                )
            aggregate_condition = _normalize_filter_expression(
                item["condition"], schema_context, query_tables
            )
            if (
                not isinstance(aggregate_condition, dict)
                or "boolean" in aggregate_condition
                or "metric" in aggregate_condition
            ):
                raise QueryGenerationError(
                    "A conditional COUNT requires one verified column predicate."
                )
        if (
            (period_condition is not None and aggregate not in {"SUM", "COUNT"})
            or (period_comparison is not None and aggregate != "SUM")
        ):
            raise QueryGenerationError(
                "Conditional periods support SUM/COUNT, and period comparisons require SUM."
            )
        distinct = item.get("distinct", False)
        if period_condition is not None and aggregate == "SUM":
            try:
                validate_period_sum_column(owner, column, schema_context)
            except ValueError as exc:
                raise QueryGenerationError(str(exc)) from exc
        if period_comparison is not None:
            try:
                validate_period_sum_column(owner, column, schema_context)
            except ValueError as exc:
                raise QueryGenerationError(str(exc)) from exc
        if period_condition is not None and aggregate == "COUNT" and normalized_joins:
            raise QueryGenerationError(
                "Conditional COUNT periods cannot be combined with joins."
            )
        if period_condition is not None and distinct:
            raise QueryGenerationError(
                "DISTINCT conditional aggregates are not supported."
            )
        date_bucket = item.get("date_bucket") if metric_name is None else None
        if date_bucket is not None:
            if (
                metric_name is not None
                or date_bucket not in _DATE_BUCKETS
                or aggregate is not None
                or _schema_column_type(schema_context, owner, column)
                not in _DATE_TYPES
            ):
                raise QueryGenerationError(
                    "date_bucket requires a plain date, datetime, or timestamp "
                    "column and must be month, quarter, or year."
                )
        if not isinstance(distinct, bool) or (
            distinct and (aggregate != "COUNT" or column == "*")
        ):
            raise QueryGenerationError(
                "Select-item distinct is supported only for COUNT of a named column; "
                "use query-level distinct for row projections."
            )
        if metric_name == "share_of_rows" and (
            aggregate is not None or distinct
        ):
            raise QueryGenerationError(
                "share_of_rows is already an aggregate and cannot be nested."
            )
        if metric_name is not None and metric_name != "share_of_rows" and (
            aggregate == "COUNT" or distinct
        ):
            raise QueryGenerationError(
                "Calculated metrics support only plain selection or SUM/AVG/MIN/MAX."
            )
        if (
            len(query_tables) > 1
            and aggregate == "COUNT"
            and not distinct
            and not count_groups
        ):
            _log_rejected_count_plan(
                "joined_count_not_distinct",
                attempt_number,
                table,
                owner,
                column,
                normalized_joins,
            )
            raise QueryGenerationError(
                "COUNT in a multi-table query must count DISTINCT entity keys to prevent join fan-out.",
                rule_id="joined_count_not_distinct",
            )
        if len(query_tables) > 1 and aggregate == "COUNT" and not count_groups:
            wording_for_intent = original_question or question
            asks_for_unique_values = bool(
                wording_for_intent
                and _DISTINCT_VALUE_COUNT_INTENT_PATTERN.search(wording_for_intent)
            )
            if (
                not _is_single_primary_key(schema_context, owner, column)
                and not asks_for_unique_values
            ):
                _log_rejected_count_plan(
                    "joined_count_not_primary_key",
                    attempt_number,
                    table,
                    owner,
                    column,
                    normalized_joins,
                )
                raise QueryGenerationError(
                    "Joined COUNT must use the single-column primary key of the entity being counted.",
                    rule_id="joined_count_not_primary_key",
                )
        if len(query_tables) > 1 and aggregate in {"SUM", "AVG", "MIN", "MAX"}:
            # Judged after the where clause is normalized (see the loop after
            # _enforce_default_address_filter).
            fanout_candidates.append((aggregate, owner, column))
        alias = item.get("alias")
        if not isinstance(alias, str) or not _VALID_ALIAS.fullmatch(alias):
            raise QueryGenerationError(f"Each selected expression needs a safe snake_case alias; got {alias!r}.")
        if alias.casefold() in aliases:
            raise QueryGenerationError(f"Duplicate selected alias {alias!r}.")
        aliases.add(alias.casefold())
        round_digits = item.get("round_digits")
        if round_digits is not None and (
            type(round_digits) is not int
            or not 0 <= round_digits <= 6
            or (
                aggregate not in {"SUM", "AVG", "MIN", "MAX"}
                and metric_name != "share_of_rows"
            )
        ):
            raise QueryGenerationError(
                "round_digits must be an integer from 0 to 6 on a numeric aggregate."
            )
        normalized_item = (
            {
                "metric": metric_name,
                "aggregate": aggregate,
                "distinct": False,
                "alias": alias,
            }
            if metric_name is not None
            else {
                "table": owner,
                "column": column,
                "aggregate": aggregate,
                "distinct": distinct,
                "alias": alias,
            }
        )
        if metric_condition is not None:
            normalized_item["metric_condition"] = metric_condition
        if aggregate_condition is not None:
            normalized_item["aggregate_condition"] = aggregate_condition
        if period_condition is not None:
            normalized_item["period"] = period_condition
        if period_comparison is not None:
            normalized_item["period_comparison"] = period_comparison
        if round_digits is not None:
            normalized_item["round_digits"] = round_digits
        if date_bucket is not None:
            normalized_item["date_bucket"] = date_bucket
        normalized_select_items.append(normalized_item)

    canonical_columns = {
        str(column).casefold()
        for source_table in (
            set(query_tables)
            | {aggregate["table"] for aggregate in isolated_child_aggregates}
        )
        for column in _columns_for_table(schema_context, source_table)
    }
    for child_aggregate in isolated_child_aggregates:
        for item in child_aggregate["select_items"]:
            alias_key = item["alias"].casefold()
            if alias_key in aliases:
                raise QueryGenerationError(
                    f"Duplicate selected alias {item['alias']!r}."
                )
            if alias_key in canonical_columns:
                raise QueryGenerationError(
                    f"Isolated metric alias {item['alias']!r} conflicts with a canonical column name."
                )
            aliases.add(alias_key)
    related_tables = {
        table,
        *(join["table"] for join in normalized_joins),
        *(item["table"] for item in normalized_exclusions),
        *(item["table"] for item in isolated_child_aggregates),
    }
    if len(related_tables) > MAX_QUERY_TABLES:
        raise QueryGenerationError(
            f"Queries may use at most {MAX_QUERY_TABLES} distinct tables, "
            "including isolated children."
        )

    if projection_distinct and any(
        item.get("aggregate") is not None or item.get("metric") is not None
        for item in normalized_select_items
    ):
        raise QueryGenerationError(
            "Projection DISTINCT cannot be combined with aggregate or metric selections."
        )

    group_by = parsed.get("group_by", [])
    if not isinstance(group_by, list):
        raise QueryGenerationError("'group_by' must be a list.")
    normalized_group_by = []
    for expression in group_by:
        if isinstance(expression, str):
            expression = {"table": table, "column": expression}
        if not isinstance(expression, dict):
            raise QueryGenerationError("Each group_by entry must identify a table and column.")
        owner, column = _resolve_column(expression, schema_context, query_tables)
        date_bucket = expression.get("date_bucket")
        if date_bucket is not None and (
            date_bucket not in _DATE_BUCKETS
            or _schema_column_type(schema_context, owner, column) not in _DATE_TYPES
        ):
            raise QueryGenerationError(
                "date_bucket requires a verified date, datetime, or timestamp column "
                "and must be month, quarter, or year."
            )
        normalized_group = {"table": owner, "column": column}
        if date_bucket is not None:
            normalized_group["date_bucket"] = date_bucket
        normalized_group_by.append(normalized_group)

    legacy_where = parsed.get("where_conditions")
    where_expression = parsed.get("where_expression")
    if legacy_where is not None and where_expression is not None:
        raise QueryGenerationError(
            "Use either where_expression or the legacy where_conditions, not both."
        )
    if where_expression is None and legacy_where is not None:
        if not isinstance(legacy_where, list):
            raise QueryGenerationError("'where_conditions' must be a list.")
        combine_with = parsed.get("combine_with", "AND")
        if combine_with not in ALLOWED_BOOLEAN_OPERATORS:
            raise QueryGenerationError(
                f"'combine_with' must be AND or OR, got {combine_with!r}."
            )
        if len(legacy_where) == 1:
            where_expression = legacy_where[0]
        elif legacy_where:
            where_expression = {
                "boolean": combine_with,
                "conditions": legacy_where,
            }
    normalized_where_expression = _normalize_filter_expression(
        where_expression, schema_context, query_tables
    )
    _validate_booking_city_governance(
        normalized_where_expression,
        normalized_group_by,
        normalized_joins,
    )
    normalized_where_expression = _enforce_at_least_one_joined_entity(
        question,
        normalized_where_expression,
        normalized_joins,
        schema_context,
        base_table=table,
    )
    normalized_where_expression = _remove_unrequested_grouped_null_exclusions(
        normalized_where_expression,
        normalized_group_by,
        " ".join(value for value in (original_question, question) if value),
        normalized_select_items,
        normalized_joins,
        schema_context,
    )
    for aggregate, owner, column in fanout_candidates:
        aggregate_on_base_table = owner == table
        all_joins_many_to_one = all(
            join_is_fanout_safe(
                join["table"],
                join["relationship"],
                normalized_where_expression,
                join.get("conditions"),
            )
            for join in normalized_joins
        )
        if aggregate_on_base_table and all_joins_many_to_one:
            continue
        join_evidence = [
            {
                "joined_table": join["table"],
                "relationship_table": join["relationship"]["table"],
                "referenced_table": join["relationship"]["referenced_table"],
                "cardinality": join["relationship"].get("cardinality"),
            }
            for join in normalized_joins
        ]
        try:
            logger.warning(
                "Node 6 rejected joined numeric aggregate plan: "
                "attempt=%r base_table=%r aggregate=%r "
                "aggregate_owner_table=%r aggregate_column=%r "
                "aggregate_on_base_table=%s "
                "all_joins_many_to_one=%s joins=%r",
                attempt_number,
                table,
                aggregate,
                owner,
                column,
                aggregate_on_base_table,
                all_joins_many_to_one,
                join_evidence,
            )
        except Exception as logging_error:
            try:
                os.write(
                    2,
                    (
                        "Node 6 aggregate diagnostic logging failed "
                        f"({type(logging_error).__name__}).\n"
                    ).encode("ascii", "replace"),
                )
            except OSError:
                pass
        raise QueryGenerationError(
            "To measure a child table or related metric, use "
            "isolated_child_aggregates instead of a standard join.",
            rule_id="fanout_select_aggregate",
        )
    _reject_ambiguous_name_filters(normalized_where_expression)
    _reject_distant_anti_join(normalized_where_expression, table, normalized_joins)
    if reject_offer_heading_as_shop_type:
        _reject_offer_heading_as_shop_type(
            normalized_where_expression,
            " ".join(value for value in (question, original_question) if value),
        )

    _enforce_default_address_filter(
        normalized_where_expression,
        query_tables,
        schema_context,
        original_question or question,
        normalized_select_items,
        normalized_joins,
        normalized_group_by,
    )

    normalized_where_expression = _normalize_booking_month_semantics(
        original_question or question,
        query_tables,
        normalized_select_items,
        normalized_group_by,
        normalized_where_expression,
        schema_context,
    )
    normalized_where_expression = _enforce_explicit_year_scope(
        original_question or question,
        normalized_group_by,
        normalized_where_expression,
        schema_context,
    )

    if (
        question
        and re.search(
            r"\b(each|per)\s+(?:the\s+)?categor(?:y|ies)(?:_id)?\b",
            question,
            re.I,
        )
        and "categories" in query_tables
    ):
        category_keys = [
            entry["column"]
            for entry in schema_context.get("categories", [])
            if isinstance(entry, dict) and entry.get("key") == "PRI"
        ]
        if len(category_keys) == 1 and not any(
            group.get("table") == "categories"
            and group.get("column") == category_keys[0]
            for group in normalized_group_by
        ):
            normalized_group_by.append({
                "table": "categories",
                "column": category_keys[0],
            })

    normalized_having_expression = _normalize_filter_expression(
        parsed.get("having_expression"),
        schema_context,
        query_tables,
        aggregate_filters=True,
        metric_aliases=set(isolated_metrics),
    )
    all_question_text = " ".join(
        value for value in (question, original_question) if value
    )
    having_leaves = _expression_leaves(normalized_having_expression)
    has_isolated_alias_having = bool(having_leaves) and all(
        leaf.get("metric_alias") is not None for leaf in having_leaves
    )
    _validate_metric_filter_units(
        normalized_where_expression,
        normalized_having_expression,
        all_question_text,
        schema_context,
        query_tables,
    )
    if _UNSUPPORTED_RATIO_PATTERN.search(all_question_text):
        raise UnanswerableQuestionError(
            "Ratios between separately counted values are not supported by the "
            "available calculated metrics."
        )
    if _RATIO_INTENT_PATTERN.search(all_question_text) and not _has_percentage_metric(
        normalized_select_items, schema_context, query_tables
    ):
        raise UnanswerableQuestionError(
            "This percentage, share, ratio, or proportion needs a supported "
            "percentage metric and a verifiable row condition."
        )
    if (
        normalized_having_expression is not None
        and not normalized_group_by
        and not (
            has_isolated_alias_having
            and isolated_child_aggregates
        )
    ):
        raise QueryGenerationError("HAVING requires a grouped query.")
    if normalized_exclusions or isolated_child_aggregates:
        if (
            normalized_group_by
            or (
                normalized_having_expression is not None
                and not has_isolated_alias_having
            )
            or count_groups
            or projection_distinct
            or any(
                item.get("aggregate") is not None or item.get("metric") is not None
                for item in normalized_select_items
            )
        ):
            raise QueryGenerationError(
                "For isolated child metrics or exclusions, select base-entity "
                "columns without GROUP BY, top-level aggregates, DISTINCT, or "
                "ordinary HAVING; move child filters into the child operation."
            )

    aggregate_items = [
        item for item in normalized_select_items
        if item["aggregate"] is not None or item.get("metric") == "share_of_rows"
    ]
    if aggregate_items:
        if any(
            item.get("metric") not in {None, "share_of_rows"}
            and item["aggregate"] is None
            for item in normalized_select_items
        ):
            raise QueryGenerationError(
                "A plain calculated metric cannot appear in a grouped aggregate query."
            )
        grouped_columns = {
            (item["table"], item["column"], item.get("date_bucket"))
            for item in normalized_group_by
        }
        for item in normalized_select_items:
            if (
                item["aggregate"] is None
                and item.get("metric") != "share_of_rows"
                and (
                    not normalized_group_by
                    or (
                        item["table"],
                        item["column"],
                        item.get("date_bucket"),
                    ) not in grouped_columns
                )
            ):
                raise QueryGenerationError(
                    "Every plain selected column in an aggregate query must appear in GROUP BY."
                )
    for item in normalized_select_items:
        if item.get("metric") == "share_of_rows" and (
            item["metric_condition"]["table"] != table
            or any(
                join["relationship"].get("cardinality") != "many_to_one"
                for join in normalized_joins
            )
        ):
            raise QueryGenerationError(
                "share_of_rows must measure base-table rows and may join only "
                "to verified many-to-one parents."
            )

    if count_groups:
        if (
            len(normalized_select_items) != 1
            or normalized_select_items[0]["aggregate"] != "COUNT"
            or normalized_select_items[0]["column"] != "*"
            or normalized_select_items[0]["distinct"]
            or not normalized_group_by
            or normalized_having_expression is None
        ):
            raise QueryGenerationError(
                "count_groups requires COUNT(*), GROUP BY, and a HAVING expression."
            )
        if normalized_where_expression and any(
            leaf.get("aggregate") for leaf in _expression_leaves(normalized_where_expression)
        ):
            raise QueryGenerationError("Aggregate predicates must be placed in HAVING.")

    count_entity = None
    joined_counts = [
        item for item in normalized_select_items
        if item["aggregate"] == "COUNT"
        and _is_single_primary_key(schema_context, item["table"], item["column"])
    ] if normalized_joins and not count_groups else []
    requested_count_entity = parsed.get("count_entity")
    if joined_counts:
        if not isinstance(requested_count_entity, dict):
            raise QueryGenerationError(
                "A joined COUNT must identify the entity whose primary key is being counted."
            )
        entity_table, entity_column = _resolve_column(
            requested_count_entity, schema_context, query_tables
        )
        entity_primary_keys = [
            column_info["column"]
            for column_info in schema_context.get(entity_table, [])
            if isinstance(column_info, dict) and column_info.get("key") == "PRI"
        ]
        if len(entity_primary_keys) != 1 or entity_column != entity_primary_keys[0]:
            raise QueryGenerationError(
                "count_entity must identify a table's single-column primary key."
            )
        if any(
            item["table"] != entity_table or item["column"] != entity_column
            for item in joined_counts
        ):
            raise QueryGenerationError(
                "Every joined COUNT must count the declared count_entity primary key."
            )
        count_entity = {"table": entity_table, "column": entity_column}
    # Treat a stray count_entity as null when the query has no joined COUNT.
    # LLMs sometimes copy this optional field into simple grouped queries; it
    # has no SQL effect and should not invalidate an otherwise safe query.

    order_by = parsed.get("order_by", [])
    if not isinstance(order_by, list):
        raise QueryGenerationError("'order_by' must be a list.")
    normalized_order_by = []
    for order in order_by:
        if not isinstance(order, dict):
            raise QueryGenerationError("Each order_by entry must be a JSON object.")
        if "metric" in order:
            if "column" in order:
                raise QueryGenerationError(
                    "order_by cannot contain both 'metric' and 'column'."
                )
            metric_name = order.get("metric")
            isolated_alias = next(
                (
                    alias for alias in isolated_metrics
                    if isinstance(metric_name, str)
                    and alias.casefold() == metric_name.casefold()
                ),
                None,
            )
            if isolated_alias is not None:
                direction = order.get("direction")
                if not isinstance(direction, str) or direction not in ALLOWED_DIRECTIONS:
                    raise QueryGenerationError(
                        f"order_by uses invalid direction '{direction}'."
                    )
                normalized_order_by.append({
                    "column": isolated_alias,
                    "direction": direction,
                })
                continue
            if metric_name != "share_of_rows":
                try:
                    validate_metric(metric_name, schema_context, query_tables)
                except ValueError as exc:
                    raise QueryGenerationError(str(exc)) from exc
            matching_metrics = [
                item for item in normalized_select_items
                if item.get("metric") == metric_name
            ]
            if len(matching_metrics) != 1:
                raise QueryGenerationError(
                    "order_by metric must identify exactly one selected metric."
                )
            direction = order.get("direction")
            if not isinstance(direction, str) or direction not in ALLOWED_DIRECTIONS:
                raise QueryGenerationError(
                    f"order_by uses invalid direction '{direction}'."
                )
            normalized_order_by.append({
                "column": matching_metrics[0]["alias"],
                "direction": direction,
            })
            continue
        column = order.get("column")
        if not isinstance(column, str) or not column:
            raise QueryGenerationError("order_by requires a column name or selected alias.")
        direction = order.get("direction")
        if not isinstance(direction, str) or direction not in ALLOWED_DIRECTIONS:
            raise QueryGenerationError(f"order_by uses invalid direction '{order.get('direction')}'.")
        if column.casefold() in aliases:
            declared_alias = next(
                (
                    item["alias"]
                    for item in normalized_select_items
                    if item["alias"].casefold() == column.casefold()
                ),
                next(
                    (
                        metric_alias
                        for metric_alias in isolated_metrics
                        if metric_alias.casefold() == column.casefold()
                    ),
                    column,
                ),
            )
            normalized_order_by.append({
                "column": declared_alias,
                "direction": direction,
            })
        else:
            owner, column = _resolve_column(order, schema_context, query_tables)
            normalized_order_by.append({
                "table": owner,
                "column": column,
                "direction": direction,
            })

    ranking_intent = parsed.get("ranking_intent")
    valid_ranking_intents = set(RANKING_INTENTS)
    if ranking_intent is None:
        if normalized_order_by and parsed.get("limit") == 1:
            ranking_intent = "single_best_with_ties"
        elif normalized_order_by and type(parsed.get("limit")) is int:
            ranking_intent = "top_n"
        else:
            ranking_intent = "none"
    if ranking_intent not in valid_ranking_intents:
        raise QueryGenerationError(
            f"ranking_intent must be one of {sorted(valid_ranking_intents)}."
        )
    ranking_policy = ranking_policy_for(ranking_intent)
    if ranking_policy["requires_order_by"] and not normalized_order_by:
        raise QueryGenerationError(
            "Ranked query intents require order_by; add a selected ranking alias "
            "and specify limit only for top_n/latest."
        )
    if (
        ranking_policy["returns_all_optimal_rows"]
        and parsed.get("limit") not in (None, 1)
    ):
        raise QueryGenerationError(
            "single_best_with_ties returns all co-optimal rows; omit limit "
            "instead of requesting a single row."
        )
    if ranking_policy["requires_positive_limit"] and (
        type(parsed.get("limit")) is not int or parsed["limit"] < 1
    ):
        raise QueryGenerationError(
            "top_n and latest ranking intents require a positive integer limit."
        )
    if ranking_policy["returns_all_optimal_rows"]:
        first_order = normalized_order_by[0]
        selected_aliases = {
            item.get("alias")
            for item in normalized_select_items
            if isinstance(item.get("alias"), str)
        }
        selected_aliases.update(
            item.get("alias")
            for aggregate in isolated_child_aggregates
            for item in aggregate.get("select_items", [])
            if isinstance(item, dict) and isinstance(item.get("alias"), str)
        )
        if first_order.get("table") is not None or first_order["column"] not in selected_aliases:
            raise QueryGenerationError(
                "single_best_with_ties requires its primary ranking expression "
                "to be a selected alias so the compiler can return every co-optimal row."
            )
    if ranking_policy["requires_stable_tie_break"]:
        stable_keys = []
        grouped_primary_keys = [
            group
            for group in normalized_group_by
            if group.get("date_bucket") is None
            and _is_single_primary_key(
                schema_context, group["table"], group["column"]
            )
        ]
        for group in grouped_primary_keys or normalized_group_by:
            if group.get("date_bucket") is not None:
                selected_group = next(
                    (
                        item
                        for item in normalized_select_items
                        if item.get("table") == group["table"]
                        and item.get("column") == group["column"]
                        and item.get("date_bucket") == group["date_bucket"]
                    ),
                    None,
                )
                if selected_group is None:
                    raise QueryGenerationError(
                        "A ranked date-bucket group must be selected for deterministic ordering."
                    )
                stable_keys.append({"alias": selected_group["alias"]})
            else:
                stable_keys.append({
                    "table": group["table"],
                    "column": group["column"],
                })
        if not stable_keys:
            primary_key = next(
                (
                    column["column"]
                    for column in schema_context[table]
                    if isinstance(column, dict) and column.get("key") == "PRI"
                ),
                None,
            )
            if primary_key is None:
                raise QueryGenerationError(
                    "A deterministic top-N query requires a primary key or grouping key."
                )
            stable_keys = [{"table": table, "column": primary_key}]
        existing_order = {
            (order.get("table"), order.get("column"))
            for order in normalized_order_by
        }
        existing_aliases = {
            order["column"].casefold()
            for order in normalized_order_by
            if order.get("table") is None
        }
        for key in stable_keys:
            if key.get("alias") is not None:
                if key["alias"].casefold() not in existing_aliases:
                    normalized_order_by.append({
                        "column": key["alias"],
                        "direction": "ASC",
                    })
                continue
            if (
                (key["table"], key["column"]) not in existing_order
                and key["column"].casefold() not in existing_aliases
            ):
                normalized_order_by.append({
                    "table": key["table"],
                    "column": key["column"],
                    "direction": "ASC",
                })

    _validate_metric_filter_units(
        normalized_where_expression,
        normalized_having_expression,
        all_question_text,
        schema_context,
        query_tables,
    )
    normalized_where_expression = _apply_savings_policy(
        normalized_where_expression,
        normalized_select_items,
        normalized_having_expression,
        normalized_order_by,
    )
    normalized_where_expression, normalized_search = _apply_name_search_filters(
        normalized_joins,
        normalized_where_expression,
        normalized_search,
        catalog_filters,
        query_tables,
    )
    normalized_where_expression = _apply_resolved_catalog_filters(
        normalized_where_expression,
        normalized_joins,
        catalog_filters,
        query_tables,
    )
    _reject_unused_left_join(
        normalized_joins,
        normalized_select_items,
        normalized_where_expression,
        normalized_group_by,
        normalized_having_expression,
        normalized_order_by,
        count_entity,
    )
    normalized_query = {
        "table": table,
        "joins": normalized_joins,
        "select_items": normalized_select_items,
        "where_expression": normalized_where_expression,
        "group_by": normalized_group_by,
        "having_expression": normalized_having_expression,
        "count_groups": count_groups,
        "distinct": projection_distinct,
        "count_entity": count_entity,
        "order_by": normalized_order_by,
        "ranking_intent": ranking_intent,
        "include_empty_groups": parsed.get("include_empty_groups") is True,
        "exclude_entities_with_rows_in": normalized_exclusions,
        "isolated_child_aggregates": isolated_child_aggregates,
        "metric_filter_unit": (
            "percentage"
            if _PERCENT_UNIT_PATTERN.search(all_question_text)
            else "currency"
            if _CURRENCY_UNIT_PATTERN.search(all_question_text)
            else None
        ),
        "reasoning": parsed["reasoning"],
    }
    if normalized_search is not None:
        normalized_query["search"] = normalized_search
    if (
        normalized_query["include_empty_groups"]
        and question is not None
        and not _EXPLICIT_EMPTY_GROUP_INTENT_PATTERN.search(question)
    ):
        raise QueryGenerationError(
            "include_empty_groups requires an explicit request for each, every, or all group members."
        )
    if normalized_query["include_empty_groups"] and any(
        "period" in item or "period_comparison" in item
        for item in normalized_select_items
    ):
        raise QueryGenerationError(
            "Conditional periods cannot be combined with include_empty_groups."
        )
    limit = parsed.get("limit")
    if limit is not None and (
        type(limit) is not int or not 1 <= limit <= MAX_USER_LIMIT
    ):
        raise QueryGenerationError(
            f"'limit' must be an integer from 1 to {MAX_USER_LIMIT}."
        )
    if limit is not None and not ranking_policy_for(
        normalized_query["ranking_intent"]
    )["returns_all_optimal_rows"]:
        normalized_query["limit"] = limit
    contracts = _select_business_semantic_contracts(
        question or "",
        matched_business_rules or [],
    )
    if contracts:
        _validate_semantic_contract(
            normalized_query,
            contracts,
            question or "",
        )
    if catalog_filters:
        try:
            validate_catalog_filter_plan(
                normalized_query,
                [
                    item for item in catalog_filters
                    if item.get("kind", "catalog") == "catalog"
                ],
            )
        except ValueError as exc:
            raise QueryGenerationError(str(exc)) from exc
    _validate_resolved_temporal_constraints(
        normalized_query,
        resolved_temporal_constraints or [],
    )
    _validate_current_state_filters(
        normalized_query,
        current_state_filters or [],
    )
    return normalized_query


def _validate_current_state_filters(
    query: Dict[str, Any],
    filters: list[Dict[str, Any]],
) -> None:
    if not filters:
        return
    predicates = _expression_leaves(query.get("where_expression"))
    for join in query.get("joins", []):
        if isinstance(join, dict):
            for condition in join.get("conditions") or []:
                predicates.extend(_expression_leaves(condition))
    for select_item in query.get("select_items", []):
        if not isinstance(select_item, dict):
            continue
        condition = select_item.get("aggregate_condition")
        if isinstance(condition, dict):
            predicates.extend(_expression_leaves(condition))

    for state_filter in filters:
        table = state_filter.get("table")
        column = state_filter.get("column")
        value = state_filter.get("value")
        if not any(
            predicate.get("table") == table
            and predicate.get("column") == column
            and predicate.get("operator") == "="
            and str(predicate.get("value")) == str(value)
            for predicate in predicates
        ):
            raise QueryGenerationError(
                "The query plan did not apply the catalog-defined current-state "
                f"filter for {state_filter.get('business_rule_term')!r}."
            )


def _verify_unanswerable_evidence(
    parsed: Dict[str, Any],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
) -> tuple[bool, str]:
    evidence = parsed.get("unanswerable_evidence")
    required_fields = {
        "missing_tables",
        "missing_columns",
        "missing_relationships",
    }
    if not isinstance(evidence, dict) or set(evidence) != required_fields:
        return (
            False,
            "An unanswerable verdict must identify unavailable tables, columns, "
            "or relationships in unanswerable_evidence.",
        )
    if any(not isinstance(evidence[field], list) for field in required_fields):
        return False, "unanswerable_evidence fields must be lists."

    confirmed_missing: list[str] = []
    contradicted: list[str] = []
    for table in evidence["missing_tables"]:
        if not isinstance(table, str) or not table.strip():
            return False, "Missing-table evidence must contain table names."
        if table in schema_context:
            contradicted.append(f"table {table!r}")
        else:
            confirmed_missing.append(f"table {table!r}")

    for item in evidence["missing_columns"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"table", "column"}
            or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("table", "column")
            )
        ):
            return False, "Missing-column evidence must identify table and column."
        table, column = item["table"], item["column"]
        available_columns = {
            entry.get("column")
            for entry in schema_context.get(table, [])
            if isinstance(entry, dict)
        }
        if column in available_columns:
            contradicted.append(f"column {table}.{column}")
        else:
            confirmed_missing.append(f"column {table}.{column}")

    edges = [
        (edge.get("table"), edge.get("referenced_table"))
        for edge in relationships or []
        if isinstance(edge, dict)
        and isinstance(edge.get("table"), str)
        and isinstance(edge.get("referenced_table"), str)
    ]
    for item in evidence["missing_relationships"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"table", "referenced_table"}
            or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("table", "referenced_table")
            )
        ):
            return (
                False,
                "Missing-relationship evidence must identify both endpoint tables.",
            )
        start, destination = item["table"], item["referenced_table"]
        if start not in schema_context or destination not in schema_context:
            confirmed_missing.append(
                f"relationship endpoint {start!r} or {destination!r}"
            )
            continue

        adjacency: dict[str, set[str]] = {}
        for left, right in edges:
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
        pending = [start]
        visited = set()
        while pending:
            current = pending.pop()
            if current == destination:
                break
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency.get(current, set()) - visited)
        if current == destination:
            contradicted.append(f"relationship path {start!r} to {destination!r}")
        else:
            confirmed_missing.append(
                f"relationship path {start!r} to {destination!r}"
            )

    if contradicted:
        return (
            False,
            "The unanswerable verdict claims unavailable resources that are "
            "present in the supplied schema or relationship graph: "
            + ", ".join(contradicted)
            + ".",
        )
    if not confirmed_missing:
        return (
            False,
            "The unanswerable verdict did not establish that a required schema "
            "resource or verified relationship is missing.",
        )
    return True, "Verified unavailable requirement(s): " + ", ".join(confirmed_missing)


def _validate_resolved_temporal_constraints(
    query: Dict[str, Any],
    constraints: list[Dict[str, Any]],
) -> None:
    if not constraints:
        return
    expression_ranges: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for predicate in _expression_leaves(query.get("where_expression")):
        table, column = predicate.get("table"), predicate.get("column")
        operator, value = predicate.get("operator"), predicate.get("value")
        if (
            isinstance(table, str)
            and isinstance(column, str)
            and isinstance(value, str)
            and operator in {">=", "<"}
        ):
            expression_ranges.setdefault((table, column), set()).add(
                (operator, value)
            )

    conditional_ranges = []
    for select_item in query.get("select_items", []):
        period = select_item.get("period")
        if isinstance(period, dict):
            conditional_ranges.append(period)
        comparison = select_item.get("period_comparison")
        if isinstance(comparison, dict):
            conditional_ranges.extend(
                item for item in comparison.values() if isinstance(item, dict)
            )

    for constraint in constraints:
        start, end = constraint["start"], constraint["end"]
        expression_applied = any(
            (">=", start) in bounds and ("<", end) in bounds
            for bounds in expression_ranges.values()
        )
        conditional_applied = any(
            item.get("start") == start and item.get("end") == end
            for item in conditional_ranges
        )
        if not expression_applied and not conditional_applied:
            raise QueryGenerationError(
                "The query plan did not apply the resolved temporal range "
                f"[{start}, {end}) for target {constraint['target']!r}."
            )


def _reject_model_supplied_subquery(value: Any) -> None:
    """Reject SQL-shaped keys before normalization can discard unknown fields."""
    forbidden_keys = {"exists", "subquery", "raw_sql", "sql"}
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in forbidden_keys:
                raise QueryGenerationError(
                    "Model-supplied SQL and subqueries are not supported; "
                    "use the typed search operation."
                )
            _reject_model_supplied_subquery(child)
    elif isinstance(value, list):
        for child in value:
            _reject_model_supplied_subquery(child)


def _verified_direct_child_relationship(
    table: Any,
    relationship: Any,
    parent_table: str,
    schema_context: Dict[str, Any],
    relationship_map: Dict[tuple[Any, Any, Any], Dict[str, Any]],
    *,
    purpose: str,
) -> Dict[str, Any]:
    if (
        not isinstance(table, str)
        or table not in schema_context
        or table == parent_table
    ):
        raise QueryGenerationError(
            f"{purpose} must name a known child table distinct from its parent; "
            "use the table that owns the child rows."
        )
    if not isinstance(relationship, dict):
        raise QueryGenerationError(
            f"{purpose} must include the exact verified child-to-base relationship "
            "from the supplied relationship catalog."
        )
    key = (
        relationship.get("constraint_name"),
        relationship.get("table"),
        relationship.get("referenced_table"),
    )
    verified = relationship_map.get(key)
    if (
        verified is None
        or verified.get("table") != table
        or verified.get("referenced_table") != parent_table
        or verified.get("cardinality") != "many_to_one"
        or not isinstance(verified.get("column_pairs"), list)
        or len(verified["column_pairs"]) != 1
    ):
        raise QueryGenerationError(
            f"{purpose} requires a direct verified many-to-one child-to-parent edge; "
            "do not traverse an intermediate table or invent a relationship."
        )
    pair = verified["column_pairs"][0]
    if (
        not isinstance(pair, dict)
        or not isinstance(pair.get("column"), str)
        or not isinstance(pair.get("referenced_column"), str)
        or not _is_single_primary_key(
            schema_context, parent_table, pair.get("referenced_column")
        )
        or pair["column"] not in _columns_for_table(schema_context, table)
    ):
        raise QueryGenerationError(
            f"{purpose} relationship must reference the parent's single-column "
            "primary key and a real child foreign-key column; select a verified edge."
        )
    return {
        "constraint_name": verified["constraint_name"],
        "table": verified["table"],
        "referenced_table": verified["referenced_table"],
        "cardinality": verified["cardinality"],
        "column_pairs": [dict(pair)],
    }


def _extract_plan_metrics(plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return isolated metrics keyed by their declared result alias."""
    metrics: Dict[str, Dict[str, Any]] = {}
    for aggregate_plan in plan.get("isolated_child_aggregates", []):
        if not isinstance(aggregate_plan, dict):
            continue
        for item in aggregate_plan.get("select_items", []):
            if not isinstance(item, dict) or not isinstance(item.get("alias"), str):
                continue
            metrics[item["alias"]] = {
                "table": aggregate_plan.get("table"),
                "column": item.get("column"),
                "aggregate": item.get("aggregate"),
                "derived_alias": aggregate_plan.get("derived_alias"),
            }
    return metrics


def _normalize_isolated_query_parts(
    parsed: Dict[str, Any],
    *,
    base_table: str,
    available_parent_tables: set[str],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
    relationship_map: Dict[tuple[Any, Any, Any], Dict[str, Any]],
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    exclusions = parsed.get("exclude_entities_with_rows_in", [])
    aggregates = parsed.get("isolated_child_aggregates", [])
    if not isinstance(exclusions, list) or not isinstance(aggregates, list):
        raise QueryGenerationError(
            "Provide exclusions and isolated_child_aggregates as JSON arrays; "
            "use an empty array when either operation is not needed."
        )
    if len(exclusions) + len(aggregates) > MAX_QUERY_TABLES - 1:
        raise QueryGenerationError(
            "Use at most four isolated child operations; combine multiple metrics "
            "from the same child table into one aggregate item."
        )

    normalized_exclusions = []
    for item in exclusions:
        if not isinstance(item, dict) or set(item) - {
            "table", "relationship", "where_expression",
        }:
            raise QueryGenerationError(
                "Each exclusion needs only table, its exact verified direct-child "
                "relationship, and an optional child-only where_expression."
            )
        child_table = item.get("table")
        relationship_key = item.get("relationship")
        relationship = (
            relationship_map.get((
                relationship_key.get("constraint_name"),
                relationship_key.get("table"),
                relationship_key.get("referenced_table"),
            ))
            if isinstance(relationship_key, dict)
            and all(
                isinstance(relationship_key.get(key), str)
                for key in ("constraint_name", "table", "referenced_table")
            )
            else None
        )
        parent_table = (
            relationship.get("referenced_table")
            if isinstance(relationship, dict)
            else None
        )
        if (
            not isinstance(parent_table, str)
            or parent_table not in available_parent_tables
        ):
            raise QueryGenerationError(
                "Entity exclusion must use a direct verified relationship to "
                "the base table or a parent table already joined in the plan; "
                "do not traverse an intermediate table."
            )
        verified = _verified_direct_child_relationship(
            child_table,
            relationship_key,
            parent_table,
            schema_context,
            relationship_map,
            purpose="Entity exclusion",
        )
        where_expression = _normalize_filter_expression(
            item.get("where_expression"),
            schema_context,
            {child_table},
        )
        normalized_exclusions.append({
            "table": child_table,
            "relationship": verified,
            "where_expression": where_expression,
        })

    normalized_aggregates = []
    used_aliases: set[str] = set()
    for index, item in enumerate(aggregates):
        if not isinstance(item, dict) or set(item) - {
            "table", "relationship", "select_items", "where_expression",
        }:
            raise QueryGenerationError(
                "Each isolated child aggregate needs table, its exact verified "
                "direct-child relationship, select_items, and an optional "
                "child-only where_expression."
            )
        child_table = item.get("table")
        verified = _verified_direct_child_relationship(
            child_table,
            item.get("relationship"),
            base_table,
            schema_context,
            relationship_map,
            purpose="Isolated child aggregate",
        )
        raw_select_items = item.get("select_items")
        if not isinstance(raw_select_items, list) or not raw_select_items:
            raise QueryGenerationError(
                "Add at least one COUNT/SUM/AVG/MIN/MAX select item to each "
                "isolated child aggregate."
            )
        child_select_items = []
        for child_item in raw_select_items:
            if (
                not isinstance(child_item, dict)
                or set(child_item) - {"column", "aggregate", "distinct", "alias"}
            ):
                raise QueryGenerationError(
                    "Use only column, aggregate, distinct, and alias in child "
                    "select_items; put child filters in where_expression."
                )
            column = child_item.get("column")
            aggregate = child_item.get("aggregate")
            if (
                not isinstance(column, str)
                or column not in _columns_for_table(schema_context, child_table)
                or aggregate not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}
            ):
                raise QueryGenerationError(
                    "Choose a real column from the child table and one supported "
                    "aggregate: COUNT, SUM, AVG, MIN, or MAX."
                )
            distinct = child_item.get("distinct", False)
            if not isinstance(distinct, bool) or (distinct and aggregate != "COUNT"):
                raise QueryGenerationError(
                    "Only COUNT may use DISTINCT in an isolated child aggregate."
                )
            if aggregate in {"SUM", "AVG"}:
                data_type = _schema_column_type(schema_context, child_table, column).lower()
                if not any(
                    numeric_type in data_type
                    for numeric_type in (
                        "int", "decimal", "numeric", "float", "double", "real",
                    )
                ):
                    raise QueryGenerationError(
                        f"{aggregate} requires a numeric column; {child_table}.{column} is {data_type!r}."
                    )
            alias = child_item.get("alias")
            if not isinstance(alias, str) or not _VALID_ALIAS.fullmatch(alias):
                raise QueryGenerationError(
                    "Give each isolated metric a unique safe snake_case alias "
                    "that does not match a schema column."
                )
            if alias.casefold() in used_aliases:
                raise QueryGenerationError(
                    f"Rename duplicate isolated metric alias {alias!r}; every "
                    "child metric must have a unique result alias."
                )
            used_aliases.add(alias.casefold())
            child_select_items.append({
                "table": child_table,
                "column": column,
                "aggregate": aggregate,
                "distinct": distinct,
                "alias": alias,
            })
        where_expression = _normalize_filter_expression(
            item.get("where_expression"),
            schema_context,
            {child_table},
        )
        normalized_aggregates.append({
            "table": child_table,
            "relationship": verified,
            "select_items": child_select_items,
            "where_expression": where_expression,
            "derived_alias": f"_isolated_child_{index}",
        })

    all_related_tables = [
        item["table"]
        for item in (*normalized_exclusions, *normalized_aggregates)
    ]
    if len(set(all_related_tables)) != len(all_related_tables):
        raise QueryGenerationError(
            "Use a child table in only one operation; combine its metrics into "
            "one isolated_child_aggregates entry. If both a metric and exclusion "
            "are required for that child, split the request into separate queries."
        )
    return normalized_exclusions, normalized_aggregates

_RETRY_NUDGE = (
    "Your previous response was invalid or incomplete. Respond again with the "
    "FULL JSON object described in the system prompt. In particular: include "
    "'reasoning' as the first field, and if the question asks for a count, "
    "total, average, sum, minimum, or maximum, make sure you used the correct "
    "aggregate function ('COUNT', 'SUM', 'AVG', 'MIN', or 'MAX') in select_items "
    "instead of selecting a plain row/column. For a joined COUNT, also include "
    "'count_entity' with the counted table and its single-column primary key. "
    "For questions about users with active subscriptions, query both users "
    "and subscriptions, filter subscriptions.status = 'active', and apply "
    "any requested users.plan_id IS NOT NULL condition. "
    "For a distinct count of shops by location, any matching shop_address "
    "qualifies. For other queries using shop_address, use is_default = 1 to "
    "avoid multiplying shop rows unless the user asks for all addresses. "
    "For monthly results in a requested year, apply the complete half-open "
    "range to the relevant date column. "
    "For a query with no joins, or with no COUNT aggregate over joins, set "
    "'count_entity' to null."
)

_RETRY_HINT_BY_RULE = {
    "shop_count_address_scope": (
        "For a distinct shop count that is not scoped by location, include every "
        "matching address and count distinct shop IDs. For a location-scoped shop "
        "count, use shop_address.is_default = 1."
    ),
    "offer_heading_not_shop_type": (
        "An offer heading is evidence only that an offer mentions those words; "
        "it does not identify the shop's business type. For shop discovery, "
        "filter verified shop name, category, or subcategory fields. Use offer "
        "headings only when the user asks about offers or deals."
    ),
    "joined_count_not_distinct": (
        "When you COUNT across joined tables, set distinct to true on that "
        "COUNT and count the counted table's single-column primary key "
        "(for example bookings.id). Set count_entity to that same table "
        "and column. If the same plan also uses SUM, AVG, MIN, or MAX on a "
        "column of the joined table, make that joined table the base table "
        "instead, join upward to its parent (for example reviews as the base "
        "joined to shop_details), group by the parent's primary key and name, "
        "and keep every aggregate on the base table's columns."
    ),
    "address_requires_default": (
        "Because the question requests primary/default address details, include "
        "shop_address.is_default = 1 as a top-level AND condition."
    ),
    "joined_count_not_primary_key": (
        "To count unique entities (for example unique users), join that "
        "entity's own table through its verified relationship and list it in "
        "joins. Then count that table's single-column primary key with "
        "distinct true, and set count_entity to the same table and column. "
        "Do not count a foreign key column such as reviews.user_id. Every "
        "table whose column you use must appear in joins."
    ),
    "fanout_select_aggregate": (
        "For a SUM, AVG, MIN, or MAX over joined tables, make the table that "
        "contains the measured column the base table. Join only upward through "
        "verified many-to-one relationships to its parent tables. Do not start "
        "from the entity table and join down to the measured rows."
    ),
    "repeated_join_table": (
        "A table may be joined only once. If the query needs counts for several "
        "values of that table, use one join and separate COUNT items with a "
        "validated condition on each item. A rejected plan is not evidence that "
        "the question is unanswerable."
    ),
}

def _is_multi_child_or_exclusion_plan(parsed: Dict[str, Any] | None) -> bool:
    if not isinstance(parsed, dict):
        return False
    if parsed.get("exclude_entities_with_rows_in"):
        return True
    if parsed.get("isolated_child_aggregates"):
        return True
    joins = parsed.get("joins")
    if isinstance(joins, list) and len(joins) > 1:
        return True
    where_leaves = (
        _expression_leaves(parsed.get("where_expression"))
        if isinstance(parsed.get("where_expression"), dict)
        else []
    )
    having_leaves = (
        _expression_leaves(parsed.get("having_expression"))
        if isinstance(parsed.get("having_expression"), dict)
        else []
    )
    for leaf in where_leaves + having_leaves:
        if isinstance(leaf, dict):
            op = leaf.get("operator")
            val = leaf.get("value")
            if op == "IS NULL" or (op == "=" and val == 0):
                return True
    return False


def _retry_nudge_for(
    error: Exception,
    parsed: Dict[str, Any] | None = None,
) -> str:
    """Generic retry message, plus the exact rejection reason and a specific hint when known."""
    rule_id = getattr(error, "rule_id", None)
    hint = _RETRY_HINT_BY_RULE.get(rule_id)
    if rule_id == "fanout_select_aggregate" and _is_multi_child_or_exclusion_plan(parsed):
        hint = (
            "Keep the primary entity table as the base table. Do not invert the base table. "
            "Use exclude_entities_with_rows_in for anti-joins and "
            "isolated_child_aggregates for child metrics."
        )
    message = f"{_RETRY_NUDGE} Your previous plan was rejected because: {error}"
    return message if hint is None else f"{message} {hint}"

# ---------------------------------------------------------------------------
# Deterministic "best rated / good reviews in <city>" plan.
# The city lookup runs first and returns shop ids; the main query then stays
# reviews -> shop_details (many-to-one only), so no fan-out is possible.
# ---------------------------------------------------------------------------
async def generate_query(
    rewritten_question: str,
    schema_context: Dict[str, Any],
    matched_business_rules: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]] | None = None,
    original_question: str | None = None,
    request_id: str | None = None,
    catalog_filters: List[Dict[str, Any]] | None = None,
    catalog_filter_combiner: str | None = None,
    resolved_temporal_constraints: List[Dict[str, Any]] | None = None,
    current_state_filters: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """
    Main entry point for Node 6.

    Returns a dict:
        {
            "table": str,
            "joins": [...],
            "select_items": [...],  # optional round_digits on numeric aggregates
            "where_expression": predicate/group | None,
            "group_by": [...],
            "having_expression": predicate/group | None,
            "count_entity": {"table": str, "column": str} | None,
            "order_by": [...],
            "limit": int,  # present only when explicitly requested
            "reasoning": str,
        }
    Ready for safety_check.py to independently re-validate and build the
    final parameterized SQL from.

    Raises:
        UnanswerableQuestionError if the LLM determined the question can't
            be answered from the available schema. Never retried.
        QueryGenerationError if the LLM call failed, or if the response
            still fails validation after MAX_ATTEMPTS tries.
    """
    questions = (rewritten_question, original_question or "")
    preflight_refusals(rewritten_question, original_question)
    has_structured_temporal_context = bool(
        resolved_temporal_constraints or current_state_filters
    )

    contracts = [] if has_structured_temporal_context else (
        _select_business_semantic_contracts(
            rewritten_question,
            matched_business_rules,
        )
    )
    if len(contracts) > 1:
        raise QueryGenerationError(
            "Multiple incompatible business semantic contracts matched this question."
        )
    if contracts:
        parsed = _build_business_contract_query(
            rewritten_question,
            schema_context,
            relationships,
            contracts[0],
        )
        validated = _validate_llm_output(
            parsed,
            schema_context,
            relationships,
            rewritten_question,
            matched_business_rules,
            catalog_filters=catalog_filters,
        )
        return validated

    messages = build_generate_query_messages(
        rewritten_question,
        schema_context,
        matched_business_rules,
        relationships,
        original_question=original_question,
        catalog_filters=catalog_filters,
        catalog_filter_combiner=catalog_filter_combiner,
        resolved_temporal_constraints=resolved_temporal_constraints,
        current_state_filters=current_state_filters,
    )
    llm = get_llm_service()
    diagnostics = _ACTIVE_QUERY_DIAGNOSTICS.get()

    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            parsed = await llm.call_agent_json("QUERY_GENERATOR", messages, temperature=0.0)
        except LLMServiceError as e:
            if diagnostics is not None:
                diagnostics["validation_attempts"].append({
                    "attempt": attempt,
                    "status": "llm_error",
                    "error": f"{type(e).__name__}: {e}",
                })
                code = infrastructure_failure_code(e)
                if code:
                    diagnostics["infrastructure_errors"].append({
                        "code": code,
                        "stage": "generate_query",
                        "detail": str(e),
                    })
            # A transport/provider failure is not the "occasionally wrong
            # shape" case this retry exists for — fail immediately, the
            # underlying LLMService already retried internally per its own
            # MAX_RETRIES policy.
            raise QueryGenerationError(f"LLM call failed during query generation: {e}") from e

        try:
            validated = _validate_llm_output(
                parsed,
                schema_context,
                relationships,
                rewritten_question,
                matched_business_rules,
                attempt_number=attempt,
                reject_offer_heading_as_shop_type=True,
                original_question=original_question,
                catalog_filters=catalog_filters,
                resolved_temporal_constraints=resolved_temporal_constraints,
                current_state_filters=current_state_filters,
            )
            if parsed.get("answerable") is True:
                for question in questions:
                    _validate_active_offer_plan(
                        validated,
                        schema_context,
                        matched_business_rules,
                        current_state_filters,
                    )
                _validate_discount_source(
                    validated, " ".join(questions), schema_context
                )
                _validate_discount_not_price_substitute(
                    validated, " ".join(questions)
                )

        except QueryGenerationError as e:
            last_error = e
            if diagnostics is not None:
                diagnostics["validation_attempts"].append({
                    "attempt": attempt,
                    "status": "validation_error",
                    "plan": {
                        key: value for key, value in parsed.items()
                        if key != "reasoning"
                    },
                    "error": str(e),
                    "rule_code": getattr(e, "rule_id", None),
                })
            # TODO: Remove temporary raw-plan diagnostics after the missing
            # offer-table validation failure has been diagnosed.
            logger.warning(
                "TEMP QUERY_GENERATOR validation failure request_id=%s "
                "attempt=%d raw_plan=%s error=%s",
                request_id,
                attempt,
                json.dumps(parsed, default=str, sort_keys=True),
                str(e),
            )
            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "Node 6 attempt %d/%d failed validation, retrying: %s",
                    attempt, MAX_ATTEMPTS, e,
                )
                messages = messages + [
                    {"role": "assistant", "content": str(parsed)},
                    {"role": "user", "content": _retry_nudge_for(e, parsed=parsed if isinstance(parsed, dict) else None)},
                ]
                if _LOG_RETRY_PROMPT:
                    logger.warning(
                        "RETRY_PROMPT request_id=%s attempt=%d rule_id=%s "
                        "validation_error=%r retry_message_sent_to_model=%r",
                        request_id, attempt, getattr(e, "rule_id", None),
                        str(e), messages[-1]["content"],
                    )
                continue
            logger.error(
                "Node 6 failed validation after %d attempt(s): %s", MAX_ATTEMPTS, e
            )
            raise QueryGenerationError(
                f"Node 6 failed validation after {MAX_ATTEMPTS} attempt(s): {e}",
                rule_id=getattr(e, "rule_id", None),
            ) from e

        if not parsed["answerable"]:
            evidence_verified, evidence_detail = _verify_unanswerable_evidence(
                parsed,
                schema_context,
                relationships,
            )
            if evidence_verified:
                if diagnostics is not None:
                    diagnostics["validation_attempts"].append({
                        "attempt": attempt,
                        "status": "unanswerable_verified",
                        "evidence": parsed["unanswerable_evidence"],
                        "verification": evidence_detail,
                    })
                logger.info(
                    "Question marked unanswerable with verified schema evidence: %s",
                    evidence_detail,
                )
                raise UnanswerableQuestionError(
                    "I couldn't find the data needed to answer this question. "
                    "Please try rephrasing it or asking about something else.",
                    internal_reasoning=str(parsed.get("reasoning", "")),
                )

            last_error = QueryGenerationError(evidence_detail)
            if diagnostics is not None:
                diagnostics["validation_attempts"].append({
                    "attempt": attempt,
                    "status": "unanswerable_unverified",
                    "plan": {
                        key: value for key, value in parsed.items()
                        if key != "reasoning"
                    },
                    "error": evidence_detail,
                })
            if attempt < MAX_ATTEMPTS:
                messages = messages + [
                    {"role": "assistant", "content": json.dumps(parsed, default=str)},
                    {
                        "role": "user",
                        "content": (
                            "Your unanswerable verdict was not supported by "
                            f"validated schema evidence: {evidence_detail} "
                            "Re-check the supplied schema and verified "
                            "relationships. If the required resources exist, "
                            "return an answerable structured query plan. If a "
                            "required resource is genuinely absent, identify it "
                            "in unanswerable_evidence."
                        ),
                    },
                ]
                continue
            raise QueryGenerationError(
                "The unanswerable verdict could not be verified after "
                f"{MAX_ATTEMPTS} attempts: {evidence_detail}",
                stage="unanswerable_verification",
            ) from last_error

        # Validation passed on this attempt.
        if diagnostics is not None:
            diagnostics["validation_attempts"].append({
                "attempt": attempt,
                "status": "validated",
                "plan": {
                    key: value for key, value in validated.items()
                    if key != "reasoning"
                },
            })

        result = validated

        if attempt > 1:
            logger.info("Node 6 succeeded on retry attempt %d", attempt)

        logger.info(
            "Generated query across %d table(s): %s",
            1 + len(result["joins"]), result["reasoning"],
        )
        return result

    # Unreachable in practice (the loop always returns or raises), but kept
    # as an explicit guard rather than letting the function fall through
    # silently if MAX_ATTEMPTS is ever set to 0 by mistake.
    raise QueryGenerationError(f"Node 6 exhausted all attempts: {last_error}")


async def repair_query_after_safety_rejection(
    rejected_query: Dict[str, Any],
    rejection_reason: str,
    rewritten_question: str,
    schema_context: Dict[str, Any],
    matched_business_rules: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]] | None = None,
    original_question: str | None = None,
    catalog_filters: List[Dict[str, Any]] | None = None,
    catalog_filter_combiner: str | None = None,
    resolved_temporal_constraints: List[Dict[str, Any]] | None = None,
    current_state_filters: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Produce one validated replacement plan for a rejected plan."""
    questions = (rewritten_question, original_question or "")
    messages = build_generate_query_messages(
        rewritten_question,
        schema_context,
        matched_business_rules,
        relationships,
        original_question=original_question,
        catalog_filters=catalog_filters,
        catalog_filter_combiner=catalog_filter_combiner,
        resolved_temporal_constraints=resolved_temporal_constraints,
        current_state_filters=current_state_filters,
    )
    messages.extend([
        {
            "role": "assistant",
            "content": json.dumps(rejected_query, default=str),
        },
        {
            "role": "user",
            "content": (
                "The previous query plan failed validation or execution for this "
                f"exact reason:\n{rejection_reason}\n"
                "Return one corrected structured query plan that resolves this "
                "specific error. Preserve the user's request and all safety "
                "constraints. Do not return SQL. If you cannot answer, provide "
                "structured unanswerable_evidence for the required missing "
                "tables, columns, or relationship path."
            ),
        },
    ])

    try:
        parsed = await get_llm_service().call_agent_json(
            "QUERY_GENERATOR", messages, temperature=0.0
        )
    except LLMServiceError as exc:
        raise QueryGenerationError(
            f"LLM call failed during query-plan repair: {exc}",
            stage="repair_llm",
        ) from exc

    if parsed.get("answerable") is False:
        evidence_verified, evidence_detail = _verify_unanswerable_evidence(
            parsed,
            schema_context,
            relationships,
        )
        if not evidence_verified:
            raise QueryGenerationError(
                "Unanswerable verdict was not verified against schema: "
                f"{evidence_detail}",
                stage="unanswerable_verification",
            )
        reasoning = str(parsed.get("reasoning", "")).strip()
        raise UnanswerableQuestionError(
            "I couldn't find the data needed to answer this question. "
            "Please try rephrasing it or asking about something else.",
            internal_reasoning=reasoning or evidence_detail,
        )

    validated = _validate_llm_output(
        parsed,
        schema_context,
        relationships,
        rewritten_question,
        matched_business_rules,
        attempt_number=1,
        reject_offer_heading_as_shop_type=True,
        original_question=original_question,
        catalog_filters=catalog_filters,
        resolved_temporal_constraints=resolved_temporal_constraints,
        current_state_filters=current_state_filters,
    )
    _validate_discount_source(validated, " ".join(questions), schema_context)
    _validate_discount_not_price_substitute(validated, " ".join(questions))
    return validated