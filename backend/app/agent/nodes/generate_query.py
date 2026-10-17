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
from difflib import SequenceMatcher
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
from app.agent.catalog_resolver import validate_catalog_filter_plan
from app.agent.search_config import normalize_shop_name_term
from app.db.mysql_client import fetch_all_readonly
from datetime import date as _date, timedelta as _timedelta
from app.agent.prompts.generate_query_prompt import (
    RATING_RANKING_POLICY,
    build_generate_query_messages,
)
from app.services.llm_service import LLMServiceError, get_llm_service

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
_VALID_ALIAS = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_RELATIVE_DATE_PATTERN = re.compile(
    r"\b(?:today|yesterday|tomorrow|expired|expires|right\s+now|as\s+of\s+now)\b"
    r"|\bat\s+the\s+moment\b"
    r"|\bcurrently\s+(?:valid|available)\b"
    r"|\b(?:this|last|previous)\s+(?:day|week|month|quarter|year)\b"
    r"|\b(?:last|past|previous)\s+\d+\s+days?\b",
    re.IGNORECASE,
)
_RATIO_INTENT_PATTERN = re.compile(
    r"%|\b(?:percent(?:age)?|proportion)\b|\bshare\s+of\b",
    re.IGNORECASE,
)
_UNSUPPORTED_RATIO_PATTERN = re.compile(r"\bratio\b", re.IGNORECASE)
_PERCENT_UNIT_PATTERN = re.compile(r"%|\bpercent(?:age)?\b", re.IGNORECASE)
_CURRENCY_UNIT_PATTERN = re.compile(
    r"₹|\b(?:rupees?|rs\.?|inr|currency|amount in)\b", re.IGNORECASE
)
_SHOP_ACTIVE_STATUS_PATTERN = re.compile(
    r"\bshops?\b.{0,100}\bcurrently\s+active\b"
    r"|\bcurrently\s+active\b.{0,100}\bshops?\b",
    re.IGNORECASE,
)
_SHOP_STATUS_COMPARISON_PATTERN = re.compile(
    r"\bactive\b.{0,120}\binactive\b|\binactive\b.{0,120}\bactive\b",
    re.IGNORECASE,
)
_ACTIVE_OFFER_INTENT_PATTERN = re.compile(
    r"\bactive(?:[- ]status)?\s+offers?\b"
    r"|\bstatus[- ]one\s+offers?\b"
    r"|\boffers?\b.{0,40}\b(?:currently\s+active|status\s+(?:is\s+)?1)\b"
    r"|\bshops?\b.{0,60}\boffer\s+status\s+(?:is\s+)?1\b",
    re.IGNORECASE,
)
_VERIFIED_ADDRESS_PATTERN = re.compile(
    r"\bverified\b.{0,40}\baddress(?:es)?\b"
    r"|\baddress(?:es)?\b.{0,40}\bverified\b",
    re.IGNORECASE,
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
_USER_ACTIVE_SUBSCRIPTION_PATTERN = re.compile(
    r"\busers?\b.{0,80}\bactive\s+subscriptions?\b"
    r"|\bactive\s+subscriptions?\b.{0,80}\busers?\b",
    re.IGNORECASE,
)
_PLAN_ASSIGNMENT_PATTERN = re.compile(
    r"\bplan\b.{0,24}\bassigned\b|\bassigned\b.{0,24}\bplan\b",
    re.IGNORECASE,
)
_SQL_INJECTION_PATTERN = re.compile(
    r"(?:;\s*(?:DROP|ALTER|TRUNCATE|DELETE|INSERT|UPDATE)\b)|(?:--\s*$)|(?:/\*.*?\*/)",
    re.IGNORECASE,
)
_PRODUCT_REVIEWS_PATTERN = re.compile(
    r"\b(?:product|products)\b.{0,60}\b(?:reviews?|ratings?)\b"
    r"|\b(?:reviews?|ratings?)\b.{0,60}\b(?:for|of)\s+(?:the\s+)?(?:products?)\b",
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

MAX_ATTEMPTS = 2  # 1 real attempt + 1 retry on validation failure


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
    Never retried — an honest "unanswerable" verdict is trusted immediately.

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

    # 1. Product review/rating queries (products are not reviewed, shops are)
    if any(_PRODUCT_REVIEWS_PATTERN.search(question) for question in questions):
        raise UnanswerableQuestionError(
            "Products do not have direct review ratings in the available database schema; "
            "reviews are attached to shops."
        )

    # 2. Shop status / verified address (case 22). This runs first because
    #    "currently active" also matches the relative-date pattern, and
    #    shop questions need their own, more accurate message.
    if any(
        _SHOP_ACTIVE_STATUS_PATTERN.search(question)
        or _VERIFIED_ADDRESS_PATTERN.search(question)
        for question in questions
    ):
        raise UnanswerableQuestionError(
            "Shop active status and verified-address meaning are not defined "
            "in the available schema and business rules."
        )

    # 2. Relative dates remain unsupported; derived metrics are handled by
    #    the code-owned metric registry and safety-checked SQL compiler.
    has_relative_time = any(
        _RELATIVE_DATE_PATTERN.search(question) for question in questions
    )
    if has_relative_time:
        raise UnanswerableQuestionError(
            "Relative dates cannot be interpreted reliably until a timezone and "
            "calendar policy are configured. Please use explicit dates."
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

def _enforce_at_least_one_joined_entity(
    question: str | None,
    where_expression: Dict[str, Any] | None,
    joins: List[Dict[str, Any]],
    schema_context: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not question:
        return where_expression

    match = re.search(
        r"\bat\s+least\s+one\s+([a-z][a-z0-9_]*)\b",
        question,
        re.IGNORECASE,
    )
    if not match:
        return where_expression

    entity = match.group(1).casefold()
    candidates = [
        join
        for join in joins
        if entity in _table_name_forms(join["table"])
    ]
    if len(candidates) != 1:
        raise QueryGenerationError(
            "An 'at least one' condition must identify exactly one joined entity table."
        )

    child_join = candidates[0]
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


def _requires_user_active_subscription(question: str) -> bool:
    return bool(_USER_ACTIVE_SUBSCRIPTION_PATTERN.search(question))


def _has_users_subscriptions_relationship(
    relationships: List[Dict[str, Any]] | None,
) -> bool:
    return any(
        {relationship.get("table"), relationship.get("referenced_table")}
        == {"users", "subscriptions"}
        for relationship in relationships or []
        if isinstance(relationship, dict)
    )


def _validate_user_active_subscription_plan(
    query: Dict[str, Any],
    question: str,
    schema_context: Dict[str, Any],
) -> None:
    """Prevent user/subscription questions from silently using a proxy field."""
    if not _requires_user_active_subscription(question):
        return
    query_tables = {query.get("table")}
    query_tables.update(
        join.get("table")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    )
    if not {"users", "subscriptions"}.issubset(query_tables):
        raise QueryGenerationError(
            "Questions about users with active subscriptions must query both "
            "users and subscriptions; a user-side status field is not a substitute."
        )

    predicates = _expression_leaves(query.get("where_expression"))
    if not any(
        predicate.get("table") == "subscriptions"
        and predicate.get("column") == "status"
        and predicate.get("operator") == "="
        and isinstance(predicate.get("value"), str)
        and predicate["value"].casefold() == "active"
        for predicate in predicates
    ):
        raise QueryGenerationError(
            "Questions about active subscriptions must filter "
            "subscriptions.status = 'active'."
        )
    if _PLAN_ASSIGNMENT_PATTERN.search(question) and not any(
        predicate.get("table") == "users"
        and predicate.get("column") == "plan_id"
        and predicate.get("operator") == "IS NOT NULL"
        for predicate in predicates
    ):
        raise QueryGenerationError(
            "Questions requiring an assigned plan must filter "
            "users.plan_id IS NOT NULL."
        )

    if _COUNT_INTENT_PATTERN.search(question):
        user_primary_keys = [
            entry.get("column")
            for entry in schema_context.get("users", [])
            if isinstance(entry, dict) and entry.get("key") == "PRI"
        ]
        if len(user_primary_keys) != 1 or not any(
            item.get("table") == "users"
            and item.get("column") == user_primary_keys[0]
            and item.get("aggregate") == "COUNT"
            and item.get("distinct") is True
            for item in query.get("select_items", [])
            if isinstance(item, dict)
        ):
            raise QueryGenerationError(
                "A joined count of users with active subscriptions must count "
                "distinct users by their single-column primary key."
            )


def _validate_active_offer_plan(
    query: Dict[str, Any],
    question: str,
    schema_context: Dict[str, Any],
) -> None:
    if not _ACTIVE_OFFER_INTENT_PATTERN.search(question):
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
    if any(
        predicate.get("table") == "shop_details"
        and predicate.get("column") == "status"
        for predicate in predicates
    ):
        raise QueryGenerationError(
            "Active-offer questions must not apply a shop_details.status filter."
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

    asks_for_review_count = bool(
        re.search(
            r"\b(?:how\s+many|count|number\s+of|total\s+reviews?)\b",
            question,
            re.IGNORECASE,
        )
        and re.search(r"\breviews?\b", question, re.IGNORECASE)
    )
    if asks_for_review_count:
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

def _build_highest_booking_value_query(
    questions: tuple[str, ...],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
) -> Dict[str, Any] | None:
    intent_pattern = re.compile(
        r"\b(?:highest|largest|greatest|maximum|max|most)\b"
        r".{0,100}\bbooking\s+value\b",
        re.I,
    )
    normalized_questions = (
        question.replace("_", " ")
        for question in questions
        if question
    )
    if not any(intent_pattern.search(question) for question in normalized_questions):
        return None
    relationship = next(
        (
            edge for edge in relationships or []
            if edge.get("constraint_name") == "reviewed_bookings_shop_id"
            and edge.get("table") == "bookings"
            and edge.get("referenced_table") == "shop_details"
            and edge.get("cardinality") == "many_to_one"
        ),
        None,
    )
    required_columns = {
        "bookings": {"id", "shop_id", "final_amount"},
        "shop_details": {"id", "name"},
    }
    if relationship is None or any(
        not columns.issubset({
            column.get("column")
            for column in schema_context.get(table, [])
            if isinstance(column, dict)
        })
        for table, columns in required_columns.items()
    ):
        return None

    return {
        "answerable": True,
        "table": "bookings",
        "joins": [{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": relationship["constraint_name"],
                "table": "bookings",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "alias": "shop_name",
            },
            {
                "table": "bookings",
                "column": "final_amount",
                "aggregate": "SUM",
                "distinct": False,
                "alias": "total_booking_value",
            },
        ],
        "where_expression": None,
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"column": "total_booking_value", "direction": "DESC"}],
        "limit": 1,
        "reasoning": (
            "Sum booking final amounts per shop, order by the total descending, "
            "and return the highest-valued shop."
        ),
    }


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


_EXPLICIT_INACTIVE_SHOP_PATTERN = re.compile(
    r"\b(?:inactive|not\s+active|deactivated|disabled)\s+shops?\b"
    r"|\bshops?\b.{0,40}\b(?:inactive|not\s+active|deactivated|disabled)\b"
    r"|\bshop\s+status\s*(?:=|is)\s*0\b",
    re.IGNORECASE,
)


def _enforce_active_shop_filter(
    where_expression: Dict[str, Any] | None,
    query_tables: set[str],
    schema_context: Dict[str, Any],
    question: str | None,
    normalized_joins: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any] | None:
    """Default shop discovery to active vendors unless inactive was requested."""
    if "shop_details" not in query_tables or question is None:
        return where_expression
    if _SHOP_STATUS_COMPARISON_PATTERN.search(question):
        return where_expression

    shop_columns = _columns_for_table(schema_context, "shop_details")
    if "status" not in shop_columns:
        raise QueryGenerationError(
            "Shop queries require shop_details.status to enforce the active-shop policy.",
            rule_id="shop_status_column_missing",
        )

    explicit_inactive = bool(
        question and _EXPLICIT_INACTIVE_SHOP_PATTERN.search(question)
    )
    required_value = 0 if explicit_inactive else 1

    shop_left_join = next(
        (
            join for join in (normalized_joins or [])
            if join.get("table") == "shop_details" and join.get("join_type") == "LEFT"
        ),
        None,
    )
    if shop_left_join is not None:
        conditions = shop_left_join.setdefault("conditions", [])
        if not any(c.get("column") == "status" for c in conditions):
            conditions.append({
                "table": "shop_details",
                "column": "status",
                "operator": "=",
                "value": required_value,
            })

        def _strip_shop_status(expr):
            if expr is None:
                return None
            if "boolean" in expr:
                cleaned = []
                for child in expr.get("conditions", []):
                    if child.get("boolean") == "OR":
                        leaves = list(_expression_leaves(child))
                        leaf_tables = {l.get("table") for l in leaves}
                        leaf_cols = {l.get("column") for l in leaves}
                        if leaf_tables == {"shop_details"} and "status" in leaf_cols and "id" in leaf_cols:
                            continue
                    s = _strip_shop_status(child)
                    if s is not None:
                        cleaned.append(s)
                if not cleaned:
                    return None
                if len(cleaned) == 1:
                    return cleaned[0]
                return {**expr, "conditions": cleaned}
            if expr.get("table") == "shop_details" and expr.get("column") == "status":
                return None
            return expr

        if where_expression and where_expression.get("boolean") == "OR":
            leaves = list(_expression_leaves(where_expression))
            leaf_tables = {l.get("table") for l in leaves}
            leaf_cols = {l.get("column") for l in leaves}
            if leaf_tables == {"shop_details"} and "status" in leaf_cols and "id" in leaf_cols:
                return None

        return _strip_shop_status(where_expression)

    status_terms = [
        term
        for term in _and_terms(where_expression)
        if term.get("table") == "shop_details" and term.get("column") == "status"
    ]
    if status_terms:
        if any(
            term.get("operator") == "="
            and str(term.get("value")) == str(required_value)
            and not isinstance(term.get("value"), bool)
            for term in status_terms
        ):
            return where_expression
        raise QueryGenerationError(
            f"Shop queries must use status = {required_value} for the requested "
            "shop status.",
            rule_id="shop_status_conflict",
        )

    active_filter = {
        "table": "shop_details",
        "column": "status",
        "operator": "=",
        "value": required_value,
    }
    if where_expression is None:
        return active_filter
    if (
        where_expression.get("boolean") == "AND"
        and isinstance(where_expression.get("conditions"), list)
    ):
        return {
            **where_expression,
            "conditions": [*where_expression["conditions"], active_filter],
        }
    return {
        "boolean": "AND",
        "conditions": [where_expression, active_filter],
    }


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


def _question_requests_all_addresses(question: str | None) -> bool:
    return bool(
        question
        and re.search(
            r"\b(?:all|every)\s+(?:of\s+the\s+)?(?:shop\s+)?addresses\b",
            question,
            re.IGNORECASE,
        )
    )


def _enforce_default_address_filter(
    where_expression: Dict[str, Any] | None,
    query_tables: set[str],
    schema_context: Dict[str, Any],
    question: str | None = None,
    select_items: List[Dict[str, Any]] | None = None,
    joins: List[Dict[str, Any]] | None = None,
) -> None:
    """Keep address joins one-row-per-shop except for distinct shop counts."""
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
    if _question_requests_all_addresses(question):
        return
    if counts_distinct_shops and not asks_primary_address:
        if any(
            term.get("table") == "shop_address"
            and term.get("column") == "is_default"
            for term in address_filters
        ):
            raise QueryGenerationError(
                "A distinct shop count by location must include any matching "
                "address; do not add an is_default filter unless requested.",
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
            "Using shop_address requires shop_address.is_default = 1 unless this "
            "is a distinct shop count or the user explicitly asks for all addresses.",
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
    enforce_active_shop_status: bool = False,
    reject_offer_heading_as_shop_type: bool = False,
    original_question: str | None = None,
    catalog_filters: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Validate and normalize the model's structured query against live schema."""
    if not isinstance(parsed, dict):
        raise QueryGenerationError("LLM response must be a JSON object.")
    _reject_model_supplied_subquery(parsed)
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
        if not isinstance(distinct, bool) or (distinct and (aggregate != "COUNT" or column == "*")):
            raise QueryGenerationError("distinct is supported only for COUNT of a named column.")
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
        if alias in aliases:
            raise QueryGenerationError(f"Duplicate selected alias {alias!r}.")
        aliases.add(alias)
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
    normalized_where_expression = _enforce_at_least_one_joined_entity(
        question,
        normalized_where_expression,
        normalized_joins,
        schema_context,
    )
    _enforce_default_address_filter(
        normalized_where_expression,
        query_tables,
        schema_context,
        original_question or question,
        normalized_select_items,
        normalized_joins,
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
            "Joined numeric aggregates must measure the base table and join only to its many-to-one parents.",
            rule_id="fanout_select_aggregate",
        )
    _reject_ambiguous_name_filters(normalized_where_expression)
    _reject_distant_anti_join(normalized_where_expression, table, normalized_joins)
    if reject_offer_heading_as_shop_type:
        _reject_offer_heading_as_shop_type(
            normalized_where_expression,
            " ".join(value for value in (question, original_question) if value),
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
    )
    all_question_text = " ".join(
        value for value in (question, original_question) if value
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
    if normalized_having_expression is not None and not normalized_group_by:
        raise QueryGenerationError("HAVING requires a grouped query.")

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
        if column in aliases:
            normalized_order_by.append({"column": column, "direction": direction})
        else:
            owner, column = _resolve_column(order, schema_context, query_tables)
            normalized_order_by.append({
                "table": owner,
                "column": column,
                "direction": direction,
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
    is_active_offer_question = bool(
        _ACTIVE_OFFER_INTENT_PATTERN.search(all_question_text)
    )
    uses_savings_metric = _query_uses_metric(
        "savings",
        normalized_select_items,
        normalized_where_expression,
        normalized_having_expression,
        normalized_order_by,
    )
    if (
        enforce_active_shop_status
        and not is_active_offer_question
        and not uses_savings_metric
    ):
        normalized_where_expression = _enforce_active_shop_filter(
            normalized_where_expression,
            query_tables,
            schema_context,
            all_question_text,
            normalized_joins=normalized_joins,
        )

    normalized_where_expression, normalized_search = _apply_name_search_filters(
        normalized_joins,
        normalized_where_expression,
        normalized_search,
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
        "count_entity": count_entity,
        "order_by": normalized_order_by,
        "include_empty_groups": parsed.get("include_empty_groups") is True,
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
    if limit is not None:
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
    return normalized_query


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
        "For a distinct count of shops by location, include every matching "
        "address and count distinct shop IDs. Do not add shop_address.is_default "
        "unless the user explicitly asks for a primary/default address."
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

def _retry_nudge_for(error: Exception) -> str:
    """Generic retry message, plus the exact rejection reason and a specific hint when known."""
    hint = _RETRY_HINT_BY_RULE.get(getattr(error, "rule_id", None))
    message = f"{_RETRY_NUDGE} Your previous plan was rejected because: {error}"
    return message if hint is None else f"{message} {hint}"

# ---------------------------------------------------------------------------
# Deterministic "best rated / good reviews in <city>" plan.
# The city lookup runs first and returns shop ids; the main query then stays
# reviews -> shop_details (many-to-one only), so no fan-out is possible.
# ---------------------------------------------------------------------------
_RANKED_RATING_PATTERN = re.compile(
    r"\b(?:best|top|highest)[\s-]*(?:rated|rating|reviewed)\b"
    r"|\bgood\s+(?:reviews?|ratings?)\b"
    r"|\bbest\s+reviews?\b"
    r"|\bwell[\s-]*(?:rated|reviewed)\b"
    r"|\bhighly[\s-]*rated\b",
    re.IGNORECASE,
)
_RANKED_EXCLUDE_PATTERN = re.compile(r"\bhow\s+many\b|\bcount\b", re.IGNORECASE)
_CITY_PATTERN = re.compile(
    r"\b(?:in|at|near|around)\s+([A-Za-z][A-Za-z .'-]{1,60}?)"
    r"(?=\s+(?:with|who|that|having|which|and|for|by)\b|[?.!,]|$)",
    re.IGNORECASE,
)
_TRADE_SUBCATEGORIES = (
    (re.compile(r"\b(?:car\s+|bike\s+|vehicle\s+|auto\s+)?mechanics?\b", re.IGNORECASE),
     "Bike & Car Servicing"),
)
_RANKED_RATING_LIMIT = 5
_MAX_CITY_SHOP_IDS = 1000
_NO_MATCH_SHOP_ID = 0  # not a real shop id; makes the query return 0 rows


def _extract_city(question: str) -> str | None:
    matches = _CITY_PATTERN.findall(question or "")
    if not matches:
        return None
    city = matches[-1].strip(" .'-")
    return city or None


def _extract_trade_subcategory(question: str) -> str | None:
    for pattern, subcategory_name in _TRADE_SUBCATEGORIES:
        if pattern.search(question or ""):
            return subcategory_name
    return None


_CITY_SIMILARITY_CUTOFF = 0.85
_MAX_CITY_VARIANTS = 10


async def _load_known_cities() -> List[str]:
    """Distinct real city names from every address. Fixed SQL, no user input."""
    rows = await fetch_all_readonly(
        "SELECT DISTINCT city FROM shop_address "
        "WHERE city IS NOT NULL AND TRIM(city) <> '' "
        "LIMIT 2000"
    )
    return [
        row[0].strip()
        for row in rows
        if row and isinstance(row[0], str) and row[0].strip()
    ]


async def _expand_city_spellings(city: str) -> List[str]:
    """The typed city plus every real city spelled almost the same way
    (e.g. Nagercoil -> Nagercoil, Nahercoil). If none is close, or the city
    list cannot be loaded, the typed city is used unchanged."""
    try:
        known = await _load_known_cities()
    except Exception as exc:
        logger.warning("City spelling lookup failed; using exact city: %s", exc)
        return [city]
    wanted = city.casefold()
    variants = sorted({
        name for name in known
        if name.casefold() == wanted
        or SequenceMatcher(None, name.casefold(), wanted).ratio()
        >= _CITY_SIMILARITY_CUTOFF
    })
    return variants[:_MAX_CITY_VARIANTS] or [city]


async def _lookup_city_shop_ids(
    cities: List[str], subcategory_name: str | None
) -> List[int]:
    """Shop ids whose DEFAULT address city is one of `cities` (optionally one
    subcategory), active shops only. The SQL text is fixed; user text only
    travels as parameters."""
    sql = (
        "SELECT DISTINCT sa.shop_id FROM shop_address sa "
        "JOIN shop_details sd ON sd.id = sa.shop_id "
    )
    params: List[Any] = []
    if subcategory_name is not None:
        sql += "JOIN subcategories sc ON sc.id = sd.subcategory_id "
    placeholders = ", ".join(["%s"] * len(cities))
    sql += (
        "WHERE sa.is_default = 1 AND sd.status = 1 "
        f"AND sa.city IN ({placeholders}) "
    )
    params.extend(cities)
    if subcategory_name is not None:
        sql += "AND sc.subcategoryname = %s "
        params.append(subcategory_name)
    sql += f"ORDER BY sa.shop_id LIMIT {_MAX_CITY_SHOP_IDS}"
    try:
        rows = await fetch_all_readonly(sql, tuple(params))
    except Exception as exc:
        raise QueryGenerationError(
            f"City lookup failed during query generation: {exc}"
        ) from exc
    ids: List[int] = []
    for row in rows:
        try:
            ids.append(int(row[0]))
        except (TypeError, ValueError, IndexError):
            continue
    return ids


def _ranked_rating_question(questions: tuple[str, ...]) -> str | None:
    """The supported ranked-shop-list question, or None."""
    return next(
        (
            q for q in questions
            if q and _ranked_rating_list_only(q)
            and _RANKED_RATING_PATTERN.search(q)
            and not _RANKED_EXCLUDE_PATTERN.search(q)
        ),
        None,
    )


_RANKED_ALLOWED_WORDS = {
    "a", "an", "are", "at", "best", "bike", "car", "find", "get", "give",
    "good", "highest", "highly", "in", "list", "me", "mechanic", "mechanics",
    "near", "need", "of", "place", "places", "rated",
    "rating", "ratings", "restaurant", "restaurants", "reviewed", "reviews",
    "shop", "shops", "show", "the", "top", "well", "which", "with",
}


def _ranked_rating_list_only(question: str) -> bool:
    """Allow only ranking/list wording, a shop kind, and one city location."""
    city = _extract_city(question)
    if city is None:
        return False
    remainder = question.casefold()
    remainder = _CITY_PATTERN.sub(" ", remainder)
    remainder = _RANKED_RATING_PATTERN.sub(" ", remainder)
    words = re.findall(r"[a-z]+", remainder)
    return bool(words) and all(word in _RANKED_ALLOWED_WORDS for word in words)


def is_ranked_rating_request(questions: tuple[str, ...]) -> bool:
    """
    True when the code-built "best rated in <city>" query will handle this
    question. The builder and the schema grader both use this one rule.
    """
    ranked_question = _ranked_rating_question(questions)
    if ranked_question is None:
        return False
    original_question = questions[1] if len(questions) > 1 else None
    if original_question and (
        not _RANKED_RATING_PATTERN.search(original_question)
        or _RANKED_EXCLUDE_PATTERN.search(original_question)
        or not _ranked_rating_list_only(original_question)
    ):
        return False
    return any(_extract_city(q) for q in questions if q) and all(
        _ranked_rating_list_only(q)
        for q in questions
        if q and _RANKED_RATING_PATTERN.search(q)
        and not _RANKED_EXCLUDE_PATTERN.search(q)
    )


async def _build_ranked_rating_query(
    questions: tuple[str, ...],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
) -> Dict[str, Any] | None:
    if not is_ranked_rating_request(questions):
        return None
    if any(_question_requires_primary_address(question) for question in questions):
        return None
    question = _ranked_rating_question(questions)
    if question is None:
        return None
    city = next((c for c in (_extract_city(q) for q in questions) if c), None)
    if city is None:
        return None  # no city: leave it to the normal path
    subcategory_name = next(
        (s for s in (_extract_trade_subcategory(q) for q in questions) if s), None
    )

    source_table, entity_table = "reviews", "shop_details"
    relationship_name = "reviewed_reviews_shop_id"
    if source_table not in schema_context or entity_table not in schema_context:
        raise UnanswerableQuestionError(
            "The required tables are not present in the verified schema."
        )
    relationship = next(
        (
            edge for edge in relationships or []
            if edge.get("constraint_name") == relationship_name
            and edge.get("table") == source_table
            and edge.get("referenced_table") == entity_table
            and edge.get("cardinality") == "many_to_one"
        ),
        None,
    )
    if relationship is None:
        raise UnanswerableQuestionError(
            "The reviewed relationship needed to rank shops is unavailable."
        )

    cities = await _expand_city_spellings(city)
    found_ids = await _lookup_city_shop_ids(cities, subcategory_name)
    shop_ids = found_ids or [_NO_MATCH_SHOP_ID]
    grader_note = None
    if found_ids:
        # Built only when real shops matched. City names come from the
        # database itself, not from user text.
        grader_note = (
            "The shop ids in the SQL were pre-filtered by the system to shops "
            "whose default address city is one of: "
            + ", ".join(f"'{name}'" for name in cities)
            + (f", and whose subcategory is '{subcategory_name}'" if subcategory_name else "")
            + ". The location filter is already applied. The system deliberately "
            "returns a ranked list of up to 5 shops (best average rating first, "
            "then most reviews), so several rows are the expected answer even "
            "when the question says 'the shop' or 'highest rating'."
        )
    return {
        "answerable": True,
        "reasoning": (
            f"{RATING_RANKING_POLICY} Rank shops in {city} by average rating, "
            "then review count. Shop ids come from the default address lookup, "
            "so no address join can duplicate reviews."
        ),
        "grader_note": grader_note,
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
        "select_items": [
            {"table": entity_table, "column": "id", "aggregate": None, "alias": "shop_id"},
            {"table": entity_table, "column": "name", "aggregate": None, "alias": "shop_name"},
            {"table": source_table, "column": "rating", "aggregate": "AVG",
             "round_digits": 2, "alias": "avg_rating"},
            {"table": source_table, "column": "id", "aggregate": "COUNT",
             "distinct": True, "alias": "review_count"},
        ],
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {"table": source_table, "column": "shop_id", "operator": "IN",
                 "value": shop_ids},
                {"table": source_table, "column": "rating", "operator": "IS NOT NULL"},
            ],
        },
        "group_by": [
            {"table": entity_table, "column": "id"},
            {"table": entity_table, "column": "name"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": source_table, "column": "id"},
        "order_by": [
            {"column": "avg_rating", "direction": "DESC"},
            {"column": "review_count", "direction": "DESC"},
            {"column": "shop_id", "direction": "ASC"},
        ],
        "limit": _RANKED_RATING_LIMIT,
    }

# ---------------------------------------------------------------------------
# Deterministic "cheap juice places in <city>" plan.
# Juice places = active shops in the city whose NAME contains "juice" (there is
# no juice subcategory). The plan lists the cheapest offers at those shops.
# offer is the base table joined only to its parent shop_details, with no
# aggregate, so no fan-out is possible.
# ---------------------------------------------------------------------------
_CHEAP_PATTERN = re.compile(
    r"\b(?:cheap|cheapest|cheaper|inexpensive|low[\s-]*priced?)\b", re.IGNORECASE
)
_JUICE_PATTERN = re.compile(r"\bjuice", re.IGNORECASE)
_CHEAP_OFFER_LIMIT = 10
_JUICE_NAME_LIKE = "%juice%"


async def _lookup_juice_shop_ids(cities: List[str]) -> List[int]:
    """Active shops in the given cities (default address) with 'juice' in the
    name. The SQL text is fixed; user text only travels as parameters."""
    placeholders = ", ".join(["%s"] * len(cities))
    sql = (
        "SELECT DISTINCT sa.shop_id FROM shop_address sa "
        "JOIN shop_details sd ON sd.id = sa.shop_id "
        "WHERE sa.is_default = 1 AND sd.status = 1 AND sd.name LIKE %s "
        f"AND sa.city IN ({placeholders}) "
        f"ORDER BY sa.shop_id LIMIT {_MAX_CITY_SHOP_IDS}"
    )
    try:
        rows = await fetch_all_readonly(sql, (_JUICE_NAME_LIKE, *cities))
    except Exception as exc:
        raise QueryGenerationError(
            f"Juice shop lookup failed during query generation: {exc}"
        ) from exc
    ids: List[int] = []
    for row in rows:
        try:
            ids.append(int(row[0]))
        except (TypeError, ValueError, IndexError):
            continue
    return ids


def is_cheap_juice_request(questions: tuple[str, ...]) -> bool:
    """
    True when the code-built cheap-juice query will handle this question.
    The builder and the schema grader both use this one rule, so they can
    never disagree.
    """
    if not any(
        q and _JUICE_PATTERN.search(q) and _CHEAP_PATTERN.search(q)
        and not _RANKED_EXCLUDE_PATTERN.search(q)
        for q in questions
    ):
        return False
    return any(_extract_city(q) for q in questions if q)


async def _build_cheap_juice_query(
    questions: tuple[str, ...],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
) -> Dict[str, Any] | None:
    if not is_cheap_juice_request(questions):
        return None
    if any(_question_requires_primary_address(question) for question in questions):
        return None
    city = next((c for c in (_extract_city(q) for q in questions) if c), None)
    if city is None:
        return None

    source_table, entity_table = "offer", "shop_details"
    relationship_name = "reviewed_offer_shop_id"
    if source_table not in schema_context or entity_table not in schema_context:
        raise UnanswerableQuestionError(
            "The required tables are not present in the verified schema."
        )
    relationship = next(
        (
            edge for edge in relationships or []
            if edge.get("constraint_name") == relationship_name
            and edge.get("table") == source_table
            and edge.get("referenced_table") == entity_table
            and edge.get("cardinality") == "many_to_one"
        ),
        None,
    )
    if relationship is None:
        raise UnanswerableQuestionError(
            "The reviewed relationship needed to list offers is unavailable."
        )

    cities = await _expand_city_spellings(city)
    found_ids = await _lookup_juice_shop_ids(cities)
    shop_ids = found_ids or [_NO_MATCH_SHOP_ID]
    grader_note = None
    if found_ids:
        grader_note = (
            "The shop ids in the SQL were pre-filtered by the system to active "
            "shops whose name contains 'juice' and whose default address city is "
            "one of: " + ", ".join(f"'{name}'" for name in cities)
            + ". The location and juice filters are already applied. A ranked "
            "list was returned deliberately: the cheapest offers (lowest price "
            "first, up to 10) at those shops, so several rows are the expected "
            "answer. This is a match on shop names and does not verify that "
            "each offer is for juice."
        )
    return {
        "answerable": True,
        "reasoning": (
            f"List the cheapest offers at juice-named shops in {city}, lowest "
            "offer price first. Shop ids come from a name and default-address "
            "lookup, so no address join can duplicate offers."
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
        "select_items": [
            {"table": entity_table, "column": "name", "aggregate": None,
             "alias": "shop_name"},
            {"table": source_table, "column": "offer_heading", "aggregate": None,
             "alias": "offer_heading"},
            {"table": source_table, "column": "offer_price", "aggregate": None,
             "alias": "offer_price"},
        ],
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {"table": source_table, "column": "shop_id", "operator": "IN",
                 "value": shop_ids},
                {"table": source_table, "column": "offer_price",
                 "operator": "IS NOT NULL"},
            ],
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [
            {"column": "offer_price", "direction": "ASC"},
            {"column": "shop_name", "direction": "ASC"},
            {"column": "offer_heading", "direction": "ASC"},
        ],
        "limit": _CHEAP_OFFER_LIMIT,
        "grader_note": grader_note,
    }


async def generate_query(
    rewritten_question: str,
    schema_context: Dict[str, Any],
    matched_business_rules: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]] | None = None,
    original_question: str | None = None,
    request_id: str | None = None,
    catalog_filters: List[Dict[str, Any]] | None = None,
    catalog_filter_combiner: str | None = None,
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

    if any(_requires_user_active_subscription(question) for question in questions):
        if not _has_users_subscriptions_relationship(relationships):
            raise UnanswerableQuestionError(
                "This question requires a verified relationship between users "
                "and subscriptions, which is not currently configured."
            )

    deterministic_plan = _build_highest_booking_value_query(
        (rewritten_question, original_question or ""),
        schema_context,
        relationships,
    )
    if deterministic_plan is not None:
        return _validate_llm_output(
            deterministic_plan,
            schema_context,
            relationships,
            rewritten_question,
            matched_business_rules,
            catalog_filters=catalog_filters,
        )
    if not _business_semantic_contracts(matched_business_rules):
        plan_questions = (rewritten_question, original_question or "")
        ranked_plan = await _build_cheap_juice_query(
            plan_questions, schema_context, relationships
        )
        if ranked_plan is None:
            ranked_plan = await _build_ranked_rating_query(
                plan_questions, schema_context, relationships
            )
        if ranked_plan is not None:
            # Taken out before validation (the validator only knows query fields),
            # then attached to the validated query for the result grader.
            grader_note = ranked_plan.pop("grader_note", None)
            validated_plan = _validate_llm_output(
                ranked_plan,
                schema_context,
                relationships,
                rewritten_question,
                matched_business_rules,
                catalog_filters=catalog_filters,
            )
            if grader_note:
                validated_plan["grader_note"] = grader_note
            return validated_plan
    contracts = _select_business_semantic_contracts(
        rewritten_question,
        matched_business_rules,
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
    )
    llm = get_llm_service()

    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            parsed = await llm.call_agent_json("QUERY_GENERATOR", messages, temperature=0.0)
        except LLMServiceError as e:
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
                enforce_active_shop_status=True,
                reject_offer_heading_as_shop_type=True,
                original_question=original_question,
                catalog_filters=catalog_filters,
            )
            if parsed.get("answerable") is True:
                for question in questions:
                    _validate_user_active_subscription_plan(
                        validated, question, schema_context
                    )
                    _validate_active_offer_plan(
                        validated, question, schema_context
                    )
                _validate_discount_source(
                    validated, " ".join(questions), schema_context
                )
                _validate_discount_not_price_substitute(
                    validated, " ".join(questions)
                )

        except QueryGenerationError as e:
            last_error = e
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
                    {"role": "user", "content": _retry_nudge_for(e)},
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

        # Validation passed on this attempt.
        if not parsed["answerable"]:
            if last_error is not None:
                raise QueryGenerationError(
                    "The query generator returned an unanswerable verdict after "
                    "a prior plan failed validation; the available data has not "
                    "been established as insufficient."
                ) from last_error
            logger.info("Question marked unanswerable: %s", parsed["reasoning"])
            raise UnanswerableQuestionError(
                "I couldn't find the data needed to answer this question. "
                "Please try rephrasing it or asking about something else.",
                internal_reasoning=parsed["reasoning"],
            )

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
) -> Dict[str, Any]:
    """Make exactly one model call to repair an allowlisted safety shape error."""
    questions = (rewritten_question, original_question or "")
    messages = build_generate_query_messages(
        rewritten_question,
        schema_context,
        matched_business_rules,
        relationships,
        original_question=original_question,
        catalog_filters=catalog_filters,
        catalog_filter_combiner=catalog_filter_combiner,
    )
    messages.extend([
        {
            "role": "assistant",
            "content": json.dumps(rejected_query, default=str),
        },
        {
            "role": "user",
            "content": (
                "Safety validation rejected the query plan for this exact reason: "
                f"{rejection_reason}\n"
                "Return one corrected structured query plan that fixes this "
                "specific ORDER BY or GROUP BY issue. Preserve the user's request "
                "and all safety constraints. Do not return SQL."
            ),
        },
    ])

    try:
        parsed = await get_llm_service().call_agent_json(
            "QUERY_GENERATOR", messages, temperature=0.0
        )
    except LLMServiceError as exc:
        raise QueryGenerationError(
            f"LLM call failed during safety-plan repair: {exc}"
        ) from exc

    if parsed.get("answerable") is False:
        reasoning = str(parsed.get("reasoning", "")).strip()
        raise UnanswerableQuestionError(
            "I couldn't find the data needed to answer this question. "
            "Please try rephrasing it or asking about something else.",
            internal_reasoning=reasoning or None,
        )

    validated = _validate_llm_output(
        parsed,
        schema_context,
        relationships,
        rewritten_question,
        matched_business_rules,
        attempt_number=1,
        enforce_active_shop_status=True,
        reject_offer_heading_as_shop_type=True,
        original_question=original_question,
        catalog_filters=catalog_filters,
    )
    for question in questions:
        _validate_user_active_subscription_plan(validated, question, schema_context)
    _validate_discount_source(validated, " ".join(questions), schema_context)
    _validate_discount_not_price_substitute(validated, " ".join(questions))
    return validated