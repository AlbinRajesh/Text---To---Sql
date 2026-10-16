"""
safety_check.py  (Node 7: Safety Check — AST-validated SQL version)

Two layers of defense, not one:
  1. BUILD: SQL is still assembled from Node 6's trusted structured dict
     using parameterized values + regex-validated identifiers (same as
     before) — this is the "we control what gets written" layer.
  2. VERIFY: The assembled SQL is then parsed into an AST with sqlglot and
     independently re-checked — confirms it is a single SELECT statement,
     rejects any write/DDL keywords structurally (not by string search),
     and cross-checks every table/column the AST actually references
     against the real cached schema (Node 3). This is the "even if Node 6
     or the builder above has a bug, this still can't produce a write or
     touch an unknown column" layer.

Both layers must pass before a query is allowed to execute.
"""

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Set, Tuple

import sqlglot
from sqlglot import exp

from app.agent.nodes.calculated_metrics import (
    compile_metric,
    compile_share_of_rows,
    validate_metric,
    validate_share_condition,
)
from app.agent.search_config import (
    MAX_SEARCH_TERM_LENGTH,
    SEARCH_ROOT_TABLES,
    SEARCHABLE_COLUMNS,
    SearchConfigurationError,
    escape_like_term,
    search_term_variants,
    validate_searchable_columns,
)
from app.agent.nodes.blocked_tables import find_blocked_tables
from app.agent.relationship_overrides import join_is_fanout_safe
from app.agent.period_aggregation import (
    normalize_period,
    normalize_period_comparison,
    validate_period_sum_column,
)

logger = logging.getLogger(__name__)
_MAX_REJECTED_PLAN_LOG_CHARS = 4096

ALLOWED_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE", "IS NULL", "IS NOT NULL", "BETWEEN"}
ALLOWED_AGGREGATES = {None, "COUNT", "SUM", "AVG", "MIN", "MAX"}
ALLOWED_DIRECTIONS = {"ASC", "DESC"}
ALLOWED_COMBINERS = {"AND", "OR"}

MAX_RESULT_LIMIT = 1000
DEFAULT_LIMIT = 200
QUERY_TIMEOUT_MS = 10_000
MAX_QUERY_TABLES = 5
MAX_FILTER_DEPTH = 8
MAX_FILTER_TERMS = 64
SENSITIVE_RESULT_COLUMNS = {
    "password",
    "remember_token",
    "fcm_token",
    "firebase_uid",
    "facebook_id",
    "otp",
    "payload",
}

# Personal contact, address and location columns, keyed by (table, column).
# They may be counted or tested for NULL, never shown, grouped, sorted or
# compared to a value. The same column names exist in public business tables
# (shop_details, shop_address, advertisements), so this must stay table-qualified.
PERSONAL_RESULT_COLUMNS = {
    ("users", "email"),
    ("users", "phone"),
    ("users", "dob"),
    ("user_address", "phone"),
    ("user_address", "buildingnumber"),
    ("user_address", "arearoadname"),
    ("user_address", "pincode"),
    ("user_address", "latitude"),
    ("user_address", "longitude"),
    ("user_locations", "latitude"),
    ("user_locations", "longitude"),
    ("bookings", "phone"),
    ("bookings", "address"),
    ("bookings", "buildingnumber"),
    ("bookings", "arearoadname"),
    ("bookings", "pincode"),
    ("bookings", "latitude"),
    ("bookings", "longitude"),
    ("login_logs", "email"),
    ("login_logs", "ip_address"),
    ("active_logins", "email"),
}
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# AST node types that must NEVER appear — structural rejection, not string search.
_FORBIDDEN_AST_TYPES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter,
    exp.Create, exp.TruncateTable, exp.Grant,
)


class SafetyViolationError(Exception):
    """Raised when a generated query fails build-time or AST-level validation."""

    def __init__(
        self,
        message: str = "",
        *,
        rule_id: str | None = None,
        stage: str = "safety_check",
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.stage = stage


REPAIRABLE_SAFETY_RULES = frozenset({
    "group_by_selected_column",
    "order_by_hidden_child_column",
    "order_by_not_grouped",
    "search_wrong_base_table",
})


# ---------------------------------------------------------------------------
# LAYER 1: build the SQL (same logic as before — unchanged behavior)
# ---------------------------------------------------------------------------

def _validate_identifier(name: str, kind: str = "identifier") -> str:
    if name == "*":
        return name
    if not isinstance(name, str) or not name or not _VALID_IDENTIFIER_PATTERN.fullmatch(name):
        raise SafetyViolationError(f"Invalid {kind} name: {name!r}. Rejected as unsafe.")
    return name


def _quote(identifier: str) -> str:
    return "*" if identifier == "*" else f"`{identifier}`"


def _qualified(table: str, column: str) -> str:
    return "*" if column == "*" else f"{_quote(table)}.{_quote(column)}"


def _normalized_state_filter(
    column: str, operator: str, value: Any, column_sql: str
) -> Tuple[str, List[str]] | None:
    if column.casefold() != "state" or operator not in {
        "=", "!=", "IN", "NOT IN",
    }:
        return None
    values = value if operator in {"IN", "NOT IN"} else [value]
    if not isinstance(values, list) or not values or any(
        not isinstance(item, str) for item in values
    ):
        return None
    normalized_values = [item.strip().replace(" ", "").lower() for item in values]
    normalized_column = f"LOWER(REPLACE(TRIM({column_sql}), ' ', ''))"
    if operator in {"=", "!="}:
        return f"{normalized_column} {operator} %s", normalized_values
    placeholders = ", ".join(["%s"] * len(normalized_values))
    return f"{normalized_column} {operator} ({placeholders})", normalized_values


def _is_aggregate_select_item(item: Dict[str, Any]) -> bool:
    return item.get("aggregate") is not None or item.get("metric") == "share_of_rows"


def _verify_metric_filter_units(query: Dict[str, Any], schema_context, query_tables):
    requested_unit = query.get("metric_filter_unit")
    if requested_unit not in {None, "currency", "percentage"}:
        raise SafetyViolationError("Invalid calculated-metric filter unit.")

    def leaves(expression):
        if not isinstance(expression, dict):
            return
        if expression.get("boolean") is not None:
            for condition in expression.get("conditions", []):
                yield from leaves(condition)
        else:
            yield expression

    if requested_unit is None:
        return
    has_percentage_metric = any(
        isinstance(item, dict)
        and item.get("metric") in {"discount_percent", "coupon_discount_percent"}
        for item in query.get("select_items", [])
    )
    for key in ("where_expression", "having_expression"):
        for condition in leaves(query.get(key)):
            metric_name = condition.get("metric")
            if metric_name is None:
                continue
            if has_percentage_metric and (
                (
                    metric_name == "final_price"
                    and condition.get("operator") == ">"
                    and condition.get("value") == Decimal("0")
                )
                or (
                    metric_name == "savings"
                    and condition.get("operator") == ">"
                    and condition.get("value") == Decimal("0")
                )
            ):
                continue
            try:
                definition = validate_metric(metric_name, schema_context, query_tables)
            except ValueError as exc:
                raise SafetyViolationError(str(exc)) from exc
            if definition.unit != requested_unit:
                raise SafetyViolationError(
                    f"A {requested_unit} threshold cannot filter a "
                    f"{definition.unit} calculated metric."
                )


def _resolve_column_table(
    item: Dict[str, Any], query_tables: List[str], schema_context: Dict[str, Any]
) -> str:
    column = item.get("column")
    table = item.get("table")
    if column == "*":
        if table is not None and table not in query_tables:
            raise SafetyViolationError(f"Unknown table '{table}' for wildcard.")
        return table or query_tables[0]
    if table is not None:
        if table not in query_tables:
            raise SafetyViolationError(f"Column references table '{table}' outside the query.")
        valid = {entry.get("column") for entry in schema_context.get(table, []) if isinstance(entry, dict)}
        if column not in valid:
            raise SafetyViolationError(f"Unknown column '{column}' on table '{table}'.")
        return table
    owners = [
        candidate for candidate in query_tables
        if column in {
            entry.get("column") for entry in schema_context.get(candidate, [])
            if isinstance(entry, dict)
        }
    ]
    if len(owners) != 1:
        raise SafetyViolationError(f"Column '{column}' must identify its table; matching tables: {owners}.")
    return owners[0]


def _build_select_clause(
    select_items: List[Dict[str, Any]],
    query_tables: List[str],
    schema_context: Dict[str, Any],
    select_params: List[Any] | None = None,
) -> str:
    select_params = select_params if select_params is not None else []
    parts = []
    for item in select_items:
        if not isinstance(item, dict):
            raise SafetyViolationError("Each selected expression must be an object.")
        metric_name = item.get("metric")
        if metric_name is not None:
            allowed_metric_keys = {
                "metric", "aggregate", "distinct", "alias", "round_digits",
                "metric_condition",
            }
            if set(item) - allowed_metric_keys:
                raise SafetyViolationError(
                    "Calculated-metric selections cannot contain model-supplied "
                    "columns or expressions."
                )
            if metric_name == "share_of_rows":
                if item.get("aggregate") is not None or item.get("distinct", False):
                    raise SafetyViolationError(
                        "share_of_rows is already an aggregate and cannot be nested."
                    )
                try:
                    checked_condition = validate_share_condition(
                        item.get("metric_condition"), schema_context, query_tables
                    )
                    if checked_condition != item.get("metric_condition"):
                        raise ValueError("share_of_rows condition is not normalized.")
                    expression = compile_share_of_rows(
                        checked_condition, schema_context, query_tables
                    )
                except ValueError as exc:
                    raise SafetyViolationError(str(exc)) from exc
            else:
                if "metric_condition" in item:
                    raise SafetyViolationError(
                        "Only share_of_rows accepts a metric condition."
                    )
                try:
                    validate_metric(metric_name, schema_context, query_tables)
                    expression = compile_metric(metric_name, schema_context, query_tables)
                except ValueError as exc:
                    raise SafetyViolationError(str(exc)) from exc
            aggregate = item.get("aggregate")
            if aggregate not in {None, "SUM", "AVG", "MIN", "MAX"}:
                raise SafetyViolationError(
                    "Calculated metrics support only SUM/AVG/MIN/MAX aggregates."
                )
            if item.get("distinct", False):
                raise SafetyViolationError(
                    "DISTINCT is not supported for calculated metrics."
                )
            if aggregate:
                expression = f"{aggregate}({expression})"
            round_digits = item.get("round_digits")
            if round_digits is None and metric_name == "share_of_rows":
                round_digits = 1
            if round_digits is not None:
                if (
                    type(round_digits) is not int
                    or not 0 <= round_digits <= 6
                    or (
                        aggregate not in {"SUM", "AVG", "MIN", "MAX"}
                        and metric_name != "share_of_rows"
                    )
                ):
                    raise SafetyViolationError(
                        "round_digits must be an integer from 0 to 6 on a numeric aggregate."
                    )
                expression = f"ROUND({expression}, {round_digits})"
            alias = item.get("alias")
            if alias:
                expression += f" AS {_quote(_validate_identifier(alias, kind='alias'))}"
            parts.append(expression)
            continue
        if "column" not in item:
            raise SafetyViolationError("Each selected expression must identify a column.")
        column = _validate_identifier(item["column"], kind="column")
        table = _resolve_column_table(item, query_tables, schema_context)
        if column.lower() in SENSITIVE_RESULT_COLUMNS:
            raise SafetyViolationError(
                f"Selecting sensitive credential column '{column}' is not allowed."
            )
        aggregate = item.get("aggregate")
        if column == "*" and aggregate is None:
            raise SafetyViolationError(
                "Plain wildcard row selection is not allowed because it may expose "
                "sensitive columns; select explicit columns."
            )
        if column == "*" and aggregate is None and any(
            str(entry.get("column", "")).lower() in SENSITIVE_RESULT_COLUMNS
            for entry in schema_context.get(table, [])
            if isinstance(entry, dict)
        ):
            raise SafetyViolationError(
                f"Wildcard selection from '{table}' could expose sensitive credentials."
            )
        if aggregate is not None and (
            not isinstance(aggregate, str) or aggregate not in ALLOWED_AGGREGATES
        ):
            raise SafetyViolationError(f"Disallowed aggregate function: {aggregate!r}")
        distinct = item.get("distinct", False)
        if not isinstance(distinct, bool) or (
            distinct and (aggregate != "COUNT" or column == "*")
        ):
            raise SafetyViolationError("DISTINCT is supported only for COUNT of a named column.")
        if len(query_tables) > 1 and aggregate == "COUNT" and not distinct:
            raise SafetyViolationError(
                "COUNT in a multi-table query must use DISTINCT on the counted entity key."
            )
        if column == "*" and aggregate is None and len(query_tables) > 1:
            raise SafetyViolationError("Wildcard selection is not allowed for multi-table queries.")

        date_bucket = item.get("date_bucket")
        if date_bucket is not None:
            if aggregate is not None or column == "*":
                raise SafetyViolationError(
                    "Date buckets are supported only for plain date/time selections."
                )
            selected_column = _date_bucket_expression(
                table, column, date_bucket, schema_context
            )
        else:
            selected_column = _qualified(table, column) if column != "*" else "*"
        period = item.get("period")
        period_comparison = item.get("period_comparison")
        aggregate_condition = item.get("aggregate_condition")
        if (
            sum(value is not None for value in (
                period, period_comparison, aggregate_condition
            )) > 1
        ):
            raise SafetyViolationError(
                "A select item may use only one conditional aggregate condition."
            )
        if (
            (period is not None and aggregate not in {"SUM", "COUNT"})
            or (period_comparison is not None and aggregate != "SUM")
            or (aggregate_condition is not None and aggregate != "COUNT")
        ):
            raise SafetyViolationError(
                "Conditional periods require SUM/COUNT, period comparisons require "
                "SUM, and row conditions require COUNT."
            )
        if period is not None and aggregate == "COUNT" and len(query_tables) > 1:
            raise SafetyViolationError(
                "Conditional COUNT periods cannot be combined with joins."
            )
        if aggregate:
            distinct_sql = "DISTINCT " if distinct else ""
            if aggregate_condition is not None:
                if (
                    not isinstance(aggregate_condition, dict)
                    or "boolean" in aggregate_condition
                    or "metric" in aggregate_condition
                    or "table" not in aggregate_condition
                    or "column" not in aggregate_condition
                ):
                    raise SafetyViolationError(
                        "Conditional COUNT requires one normalized column predicate."
                    )
                condition_sql, condition_params = _build_filter_expression(
                    aggregate_condition, query_tables, schema_context
                )
                if not condition_sql:
                    raise SafetyViolationError(
                        "Conditional COUNT requires a non-empty column predicate."
                    )
                conditional_value = "1" if column == "*" else selected_column
                expr = (
                    f"COUNT({distinct_sql}CASE WHEN {condition_sql} "
                    f"THEN {conditional_value} ELSE NULL END)"
                )
                select_params.extend(condition_params)
            elif period is not None or period_comparison is not None:
                try:
                    if period is not None:
                        checked_period = normalize_period(
                            period, schema_context, query_tables
                        )
                        checked_comparison = None
                    else:
                        checked_comparison = normalize_period_comparison(
                            period_comparison, schema_context, query_tables
                        )
                        checked_period = None
                except ValueError as exc:
                    raise SafetyViolationError(str(exc)) from exc
                if aggregate not in {"SUM", "COUNT"} or distinct:
                    raise SafetyViolationError(
                        "Conditional periods support only non-distinct SUM and COUNT."
                    )
                if aggregate == "SUM":
                    try:
                        validate_period_sum_column(table, column, schema_context)
                    except ValueError as exc:
                        raise SafetyViolationError(str(exc)) from exc
                if checked_comparison is not None:
                    if aggregate != "SUM":
                        raise SafetyViolationError(
                            "Period comparisons support only SUM aggregation."
                        )
                    later = checked_comparison["later_period"]
                    earlier = checked_comparison["earlier_period"]
                    date_column = _qualified(later["table"], later["column"])
                    expr = (
                        f"SUM(CASE WHEN {date_column} >= %s AND {date_column} < %s "
                        f"THEN {selected_column} ELSE NULL END) - "
                        f"SUM(CASE WHEN {date_column} >= %s AND {date_column} < %s "
                        f"THEN {selected_column} ELSE NULL END)"
                    )
                    select_params.extend([
                        later["start"], later["end"],
                        earlier["start"], earlier["end"],
                    ])
                else:
                    date_column = _qualified(
                        checked_period["table"], checked_period["column"]
                    )
                    conditional_value = "1" if aggregate == "COUNT" and column == "*" else selected_column
                    expr = (
                        f"{aggregate}(CASE WHEN {date_column} >= %s AND "
                        f"{date_column} < %s THEN {conditional_value} ELSE NULL END)"
                    )
                    select_params.extend(
                        [checked_period["start"], checked_period["end"]]
                    )
            else:
                expr = f"{aggregate}({distinct_sql}{selected_column})"
        else:
            expr = selected_column
        round_digits = item.get("round_digits")
        if round_digits is not None:
            if (
                type(round_digits) is not int
                or not 0 <= round_digits <= 6
                or aggregate not in {"SUM", "AVG", "MIN", "MAX"}
            ):
                raise SafetyViolationError(
                    "round_digits must be an integer from 0 to 6 on a numeric aggregate."
                )
            expr = f"ROUND({expr}, {round_digits})"

        alias = item.get("alias")
        if alias:
            safe_alias = _validate_identifier(alias, kind="alias")
            expr = f"{expr} AS {_quote(safe_alias)}"

        parts.append(expr)
    return ", ".join(parts)

def _build_where_clause(
    where_conditions: List[Dict[str, Any]],
    combine_with: str,
    query_tables: List[str],
    schema_context: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    if not where_conditions:
        return "", []

    if combine_with not in ALLOWED_COMBINERS:
        raise SafetyViolationError(f"Disallowed combiner: {combine_with!r}")

    clauses = []
    params: List[Any] = []

    for cond in where_conditions:
        if not isinstance(cond, dict) or "column" not in cond:
            raise SafetyViolationError("Each filter must identify a column.")
        column = _validate_identifier(cond["column"], kind="column")
        table = _resolve_column_table(cond, query_tables, schema_context)
        operator = cond["operator"]
        if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
            raise SafetyViolationError(f"Disallowed operator: {operator!r}")

        value = cond.get("value")
        quoted_column = _qualified(table, column)

        word_filter = _shop_name_word_filter(table, column, operator, value, quoted_column)
        if word_filter is not None:
            clauses.append(word_filter[0])
            params.extend(word_filter[1])
            continue
        normalized_state = _normalized_state_filter(
            column, operator, value, quoted_column
        )
        if normalized_state is not None:
            clauses.append(normalized_state[0])
            params.extend(normalized_state[1])
            continue

        if operator in ("IS NULL", "IS NOT NULL"):
            clauses.append(f"{quoted_column} {operator}")
        elif operator == "BETWEEN":
            if not isinstance(value, list) or len(value) != 2:
                raise SafetyViolationError(f"BETWEEN requires a [low, high] value list. Got: {value!r}")
            clauses.append(f"{quoted_column} BETWEEN %s AND %s")
            params.extend(value)
        elif operator in ("IN", "NOT IN"):
            if not isinstance(value, list) or not value:
                raise SafetyViolationError(f"{operator} requires a non-empty value list. Got: {value!r}")
            placeholders = ", ".join(["%s"] * len(value))
            clauses.append(f"{quoted_column} {operator} ({placeholders})")
            params.extend(value)
        else:
            clauses.append(f"{quoted_column} {operator} %s")
            params.append(value)

    joiner = f" {combine_with} "
    return joiner.join(clauses), params

# Shop names are searched word by word: "kingstar juice" must find
# "kINGSTAR FRESH JUICE POINT". Set to False to restore the old behaviour.
SHOP_NAME_WORD_MATCH = True
_MAX_NAME_WORDS = 5


def _escape_like_word(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _shop_name_word_filter(
    table: str, column: str, operator: str, value: Any, column_sql: str
) -> Tuple[str, List[Any]] | None:
    """
    Turns a shop-name filter into one LIKE per word, joined with AND, so the
    words may appear anywhere and in any order in the stored name.
    Returns None when the filter is not a shop-name text search (the caller
    then builds the filter the normal way).
    """
    if (
        not SHOP_NAME_WORD_MATCH
        or table != "shop_details"
        or column != "name"
        or operator not in ("=", "LIKE")
        or not isinstance(value, str)
    ):
        return None

    words: List[str] = []
    seen = set()
    for word in value.replace("%", " ").split():
        key = word.casefold()
        if len(word) >= 2 and key not in seen:
            seen.add(key)
            words.append(word)
    if not words:
        return None

    words = words[:_MAX_NAME_WORDS]
    clause = " AND ".join([f"{column_sql} LIKE %s"] * len(words))
    return f"({clause})", [f"%{_escape_like_word(word)}%" for word in words]

def _build_filter_expression(
    expression: Dict[str, Any] | None,
    query_tables: List[str],
    schema_context: Dict[str, Any],
    *,
    aggregate_filters: bool = False,
) -> Tuple[str, List[Any]]:
    if expression is None:
        return "", []

    terms = 0

    def visit(node: Dict[str, Any], depth: int) -> Tuple[str, List[Any]]:
        nonlocal terms
        if depth > MAX_FILTER_DEPTH:
            raise SafetyViolationError(
                f"Filter expression exceeds maximum depth {MAX_FILTER_DEPTH}."
            )
        if not isinstance(node, dict):
            raise SafetyViolationError("Each filter expression must be an object.")

        boolean = node.get("boolean")
        if boolean is not None:
            if set(node) != {"boolean", "conditions"}:
                raise SafetyViolationError(
                    "Boolean filters may contain only AND/OR conditions; "
                    "model-supplied subqueries are not allowed."
                )
            conditions = node.get("conditions")
            if (
                boolean not in ALLOWED_COMBINERS
                or not isinstance(conditions, list)
                or not conditions
            ):
                raise SafetyViolationError(
                    "Boolean filter groups need AND/OR and at least one condition."
                )
            if len(conditions) == 1:
                return visit(conditions[0], depth + 1)
            compiled = [visit(condition, depth + 1) for condition in conditions]
            sql = f" {boolean} ".join(f"({part[0]})" for part in compiled)
            params = [value for _, part_params in compiled for value in part_params]
            return sql, params

        if set(node) - {
            "table", "column", "operator", "value", "aggregate", "metric",
        }:
            raise SafetyViolationError(
                "Filter predicates may contain only validated column conditions; "
                "model-supplied subqueries are not allowed."
            )
        terms += 1
        if terms > MAX_FILTER_TERMS:
            raise SafetyViolationError(
                f"Filter expression exceeds maximum of {MAX_FILTER_TERMS} conditions."
            )
        metric_name = node.get("metric")
        if metric_name is not None:
            allowed_metric_keys = {"metric", "operator", "value"}
            if aggregate_filters:
                allowed_metric_keys.add("aggregate")
            if set(node) - allowed_metric_keys:
                raise SafetyViolationError(
                    "Calculated-metric filters cannot contain model-supplied "
                    "columns or expressions."
                )
            try:
                validate_metric(metric_name, schema_context, query_tables)
                expression_sql = compile_metric(
                    metric_name, schema_context, query_tables
                )
            except ValueError as exc:
                raise SafetyViolationError(str(exc)) from exc
        else:
            column = _validate_identifier(node.get("column"), kind="column")
            table = _resolve_column_table(node, query_tables, schema_context)
            expression_sql = _qualified(table, column)
        operator = node.get("operator")
        if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
            raise SafetyViolationError(f"Disallowed filter operator: {operator!r}")
        value = node.get("value")
        if aggregate_filters:
            aggregate = node.get("aggregate")
            allowed_metric_aggregates = {"SUM", "AVG", "MIN", "MAX"}
            allowed_aggregates = (
                allowed_metric_aggregates
                if metric_name is not None
                else ALLOWED_AGGREGATES - {None}
            )
            if aggregate not in allowed_aggregates:
                raise SafetyViolationError(f"Disallowed HAVING aggregate: {aggregate!r}")
            expression_sql = f"{aggregate}({expression_sql})"

        if not aggregate_filters and metric_name is None:
            word_filter = _shop_name_word_filter(table, column, operator, value, expression_sql)
            if word_filter is not None:
                return word_filter
            normalized_state = _normalized_state_filter(
                column, operator, value, expression_sql
            )
            if normalized_state is not None:
                return normalized_state
        if metric_name is not None and operator not in {"IS NULL", "IS NOT NULL"}:
            metric_values = (
                value if operator in {"IN", "NOT IN", "BETWEEN"} else [value]
            )
            if (
                not isinstance(metric_values, list)
                or not metric_values
                or (operator == "BETWEEN" and len(metric_values) != 2)
                or any(isinstance(item, (dict, list, bool)) for item in metric_values)
            ):
                raise SafetyViolationError(
                    f"{operator} requires finite numeric calculated-metric values."
                )
            try:
                numeric_values = [Decimal(str(item)) for item in metric_values]
                if any(not number.is_finite() for number in numeric_values):
                    raise InvalidOperation
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise SafetyViolationError(
                    "Calculated-metric filter values must be finite numbers."
                ) from exc
            value = (
                numeric_values
                if operator in {"IN", "NOT IN", "BETWEEN"}
                else numeric_values[0]
            )

        if operator in {"IS NULL", "IS NOT NULL"}:
            if "value" in node:
                raise SafetyViolationError(f"{operator} must not include a value.")
            return f"{expression_sql} {operator}", []
        if operator == "BETWEEN":
            if not isinstance(value, list) or len(value) != 2:
                raise SafetyViolationError("BETWEEN requires exactly two values.")
            if any(isinstance(item, (dict, list)) for item in value):
                raise SafetyViolationError("BETWEEN values must be scalars.")
            return f"{expression_sql} BETWEEN %s AND %s", value
        if operator in {"IN", "NOT IN"}:
            if not isinstance(value, list) or not value:
                raise SafetyViolationError(f"{operator} requires a non-empty list.")
            if any(isinstance(item, (dict, list)) for item in value):
                raise SafetyViolationError(f"{operator} values must be scalars.")
            placeholders = ", ".join(["%s"] * len(value))
            return f"{expression_sql} {operator} ({placeholders})", value
        if isinstance(value, (dict, list)):
            raise SafetyViolationError(f"{operator} requires a scalar value.")
        return f"{expression_sql} {operator} %s", [value]

    return visit(expression, 0)


def _build_search_expression(
    search: Any,
    base_table: str,
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]],
) -> Tuple[str, List[Any]]:
    if search is None:
        return "", []
    if not isinstance(search, dict) or set(search) != {"term", "scope"}:
        raise SafetyViolationError(
            "The typed search operation must contain exactly 'term' and 'scope'."
        )
    term = search.get("term")
    scope = search.get("scope")
    if (
        not isinstance(term, str)
        or not term.strip()
        or len(term) > MAX_SEARCH_TERM_LENGTH
    ):
        raise SafetyViolationError(
            f"Search term must contain 1 to {MAX_SEARCH_TERM_LENGTH} characters."
        )
    if not isinstance(scope, str) or scope not in SEARCHABLE_COLUMNS:
        raise SafetyViolationError(f"Unsupported search scope {scope!r}.")
    root_table = SEARCH_ROOT_TABLES[scope]
    if base_table != root_table:
        raise SafetyViolationError(
            f"Typed search scope {scope!r} requires base table {root_table!r}; "
            f"the plan used {base_table!r}. Regenerate the plan with the required "
            "search root table.",
            rule_id="search_wrong_base_table",
        )
    try:
        validate_searchable_columns(schema_context, relationships)
    except SearchConfigurationError as exc:
        raise SafetyViolationError(
            f"Search configuration is not valid for this schema: {exc}"
        ) from exc

    terms = search_term_variants(term.strip())
    patterns = [
        f"%{escape_like_term(value.lower())}%"
        for value in terms
    ]
    field_clauses = []
    params: List[Any] = []
    for field in SEARCHABLE_COLUMNS[scope]:
        table = field["table"]
        column = _validate_identifier(field["column"], kind="search column")
        column_sql = _qualified(table, column)
        match_sql = " OR ".join(
            f"LOWER({column_sql}) LIKE %s ESCAPE '!'" for _ in patterns
        )
        params.extend(patterns)

        selected_relationship = field["relationship"]
        if selected_relationship is None:
            if table != base_table:
                raise SafetyViolationError(
                    "A direct search column must belong to the query's base table."
                )
            field_clauses.append(f"({match_sql})")
            continue

        relationship = next(
            (
                item
                for item in relationships
                if (
                    item.get("constraint_name"),
                    item.get("table"),
                    item.get("referenced_table"),
                )
                == (
                    selected_relationship["constraint_name"],
                    selected_relationship["table"],
                    selected_relationship["referenced_table"],
                )
            ),
            None,
        )
        if relationship is None:
            raise SafetyViolationError(
                f"Search join for {table}.{column} is not verified."
            )
        equality_parts = []
        for pair in relationship.get("column_pairs", []):
            if relationship["table"] == base_table:
                root_column = pair["column"]
                field_column = pair["referenced_column"]
            else:
                root_column = pair["referenced_column"]
                field_column = pair["column"]
            equality_parts.append(
                f"{_qualified(base_table, _validate_identifier(root_column, 'search join column'))} = "
                f"{_qualified(table, _validate_identifier(field_column, 'search join column'))}"
            )
        if not equality_parts:
            raise SafetyViolationError(
                f"Search join for {table}.{column} has no verified column pairs."
            )
        field_clauses.append(
            f"EXISTS (SELECT 1 FROM {_quote(table)} "
            f"WHERE ({' AND '.join(equality_parts)}) AND ({match_sql}))"
        )

    if not field_clauses:
        raise SafetyViolationError(f"Search scope {scope!r} has no enabled fields.")
    return "(" + " OR ".join(field_clauses) + ")", params


def _build_query_where_clause(
    query: Dict[str, Any],
    query_tables: List[str],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]],
) -> Tuple[str, List[Any]]:
    where_expression = query.get("where_expression")
    if where_expression is not None:
        if query.get("where_conditions"):
            raise SafetyViolationError(
                "Use either where_expression or legacy where_conditions, not both."
            )
        where_clause, where_params = _build_filter_expression(
            where_expression, query_tables, schema_context
        )
    else:
        where_clause, where_params = _build_where_clause(
            query.get("where_conditions", []),
            query.get("combine_with", "AND"),
            query_tables,
            schema_context,
        )

    search_clause, search_params = _build_search_expression(
        query.get("search"),
        query["table"],
        schema_context,
        relationships,
    )
    if search_clause:
        where_clause = (
            f"({where_clause}) AND ({search_clause})"
            if where_clause
            else search_clause
        )
        where_params.extend(search_params)
    return where_clause, where_params


def _build_group_by_clause(
    group_by: List[Any], query_tables: List[str], schema_context: Dict[str, Any]
) -> str:
    if not group_by:
        return ""
    columns = []
    for group in group_by:
        if isinstance(group, str):
            group = {"column": group}
        if not isinstance(group, dict) or "column" not in group:
            raise SafetyViolationError("Each grouping expression must identify a column.")
        column = _validate_identifier(group["column"], kind="column")
        table = _resolve_column_table(group, query_tables, schema_context)
        date_bucket = group.get("date_bucket")
        if date_bucket is not None:
            columns.append(
                _date_bucket_expression(table, column, date_bucket, schema_context)
            )
            continue
        data_type = next(
            (
                str(entry.get("data_type", "")).lower()
                for entry in schema_context.get(table, [])
                if isinstance(entry, dict) and entry.get("column") == column
            ),
            "",
        )
        if data_type in {"timestamp", "datetime"}:
            raise SafetyViolationError(
                f"Grouping by the raw time column '{table}.{column}' is not "
                "supported; it would create one group per second.",
                rule_id="group_by_raw_timestamp",
            )
        columns.append(_qualified(table, column))
    return ", ".join(columns)


def _date_bucket_expression(
    table: str,
    column: str,
    bucket: Any,
    schema_context: Dict[str, Any],
) -> str:
    allowed_buckets = {"month", "quarter", "year"}
    if bucket not in allowed_buckets:
        raise SafetyViolationError(
            "Date buckets must be one of month, quarter, or year."
        )
    data_type = next(
        (
            str(entry.get("data_type", "")).lower()
            for entry in schema_context.get(table, [])
            if isinstance(entry, dict) and entry.get("column") == column
        ),
        "",
    )
    if data_type not in {"date", "datetime", "timestamp"}:
        raise SafetyViolationError(
            "Date buckets require a schema-verified date or timestamp column."
        )
    source = _qualified(table, column)
    if bucket == "month":
        return (
            f"CONCAT(YEAR({source}), '-', "
            f"LPAD(MONTH({source}), 2, '0'))"
        )
    if bucket == "quarter":
        return f"CONCAT(YEAR({source}), '-Q', QUARTER({source}))"
    return f"YEAR({source})"


def _validate_date_bucket_plan(
    select_items: List[Dict[str, Any]],
    group_by: List[Any],
) -> None:
    groups = {
        (group.get("table"), group.get("column"), group.get("date_bucket"))
        for group in group_by
        if isinstance(group, dict)
    }
    for item in select_items:
        if not isinstance(item, dict) or item.get("metric") is not None:
            continue
        selected_bucket = item.get("date_bucket")
        key = (item.get("table"), item.get("column"), selected_bucket)
        same_source_groups = [
            group for group in groups
            if group[0] == item.get("table") and group[1] == item.get("column")
        ]
        if selected_bucket is not None and key not in groups:
            raise SafetyViolationError(
                "A selected date bucket must match a GROUP BY date bucket."
            )
        if (
            selected_bucket is None
            and item.get("aggregate") is None
            and any(group[2] is not None for group in same_source_groups)
        ):
            raise SafetyViolationError(
                "A date bucket grouping must select the same bucket, not the raw date column."
            )


def _build_order_by_clause(
    order_by: List[Dict[str, str]],
    query_tables: List[str],
    schema_context: Dict[str, Any],
    aliases: Set[str],
    select_items: List[Dict[str, Any]] | None = None,
    group_by: List[Any] | None = None,
) -> str:
    if not order_by:
        return ""
    parts = []
    for order in order_by:
        if not isinstance(order, dict) or "column" not in order:
            raise SafetyViolationError("Each ordering expression must identify a column.")
        column = _validate_identifier(order["column"], kind="column")
        if column in aliases:
            sort_column = _quote(column)
            selected_item = next(
                (
                    item
                    for item in select_items or []
                    if item.get("alias") == column
                    and item.get("aggregate") is None
                    and item.get("metric") is None
                ),
                None,
            )
            if (
                selected_item is not None
                and selected_item.get("date_bucket") is None
                and selected_item.get("column", "").lower().endswith("_month")
                and any(
                    isinstance(group, dict)
                    and group.get("table") == selected_item.get("table")
                    and group.get("column") == selected_item.get("column")
                    and group.get("date_bucket") is None
                    for group in group_by or []
                )
            ):
                owner = _resolve_column_table(
                    selected_item, query_tables, schema_context
                )
                month_column = selected_item["column"]
                prefix = month_column[:-len("_month")]
                companion_date = next(
                    (
                        candidate
                        for candidate in (f"{prefix}_date", f"{prefix}_at")
                        if any(
                            isinstance(entry, dict)
                            and entry.get("column") == candidate
                            and str(entry.get("data_type", "")).lower()
                            in {"date", "datetime", "timestamp"}
                            for entry in schema_context.get(owner, [])
                        )
                    ),
                    None,
                )
                if companion_date is not None:
                    sort_column = f"MIN({_qualified(owner, companion_date)})"
        else:
            table = _resolve_column_table(order, query_tables, schema_context)
            sort_column = _qualified(table, column)
        direction = order.get("direction", "ASC")
        if not isinstance(direction, str) or direction not in ALLOWED_DIRECTIONS:
            raise SafetyViolationError(f"Disallowed sort direction: {direction!r}")
        parts.append(f"{sort_column} {direction}")
    return ", ".join(parts)


def _relationship_matches(
    candidate: Dict[str, Any], selected: Dict[str, Any]
) -> bool:
    return (
        candidate.get("constraint_name") == selected.get("constraint_name")
        and candidate.get("table") == selected.get("table")
        and candidate.get("referenced_table") == selected.get("referenced_table")
    )


def _combine_filter_nodes(
    boolean: str, expressions: List[Dict[str, Any]]
) -> Dict[str, Any] | None:
    if not expressions:
        return None
    if len(expressions) == 1:
        return expressions[0]
    return {"boolean": boolean, "conditions": expressions}


def _partition_empty_group_filters(
    expression: Any,
    grouping_table: str,
    query_tables: Set[str],
) -> Tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    """Split conjunctive predicates between the outer dimension and fact query."""
    if expression is None:
        return None, None
    if not isinstance(expression, dict):
        raise SafetyViolationError(
            "include_empty_groups requires a validated filter expression."
        )

    boolean = expression.get("boolean")
    if boolean is None:
        table = expression.get("table")
        if table not in query_tables:
            raise SafetyViolationError(
                "include_empty_groups filter references a table outside its join path."
            )
        if table == grouping_table:
            return expression, None
        return None, expression

    conditions = expression.get("conditions")
    if (
        boolean not in ALLOWED_COMBINERS
        or not isinstance(conditions, list)
        or not conditions
    ):
        raise SafetyViolationError(
            "include_empty_groups requires non-empty AND/OR filter groups."
        )

    partitions = [
        _partition_empty_group_filters(condition, grouping_table, query_tables)
        for condition in conditions
    ]
    if boolean == "AND":
        return (
            _combine_filter_nodes("AND", [outer for outer, _ in partitions if outer]),
            _combine_filter_nodes("AND", [inner for _, inner in partitions if inner]),
        )

    if any(outer is not None and inner is not None for outer, inner in partitions):
        raise SafetyViolationError(
            "An OR filter cannot mix grouping-entity and fact-side predicates "
            "when including empty groups."
        )
    if all(outer is not None for outer, _ in partitions):
        return _combine_filter_nodes("OR", [outer for outer, _ in partitions if outer]), None
    if all(inner is not None for _, inner in partitions):
        return None, _combine_filter_nodes("OR", [inner for _, inner in partitions if inner])
    raise SafetyViolationError(
        "An OR filter cannot mix grouping-entity and fact-side predicates "
        "when including empty groups."
    )


def _prepare_empty_group_plan(
    query: Dict[str, Any],
    valid_tables: List[str],
    schema_context: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate the narrow safe shape used to include entities with no facts."""
    if query.get("count_groups", False):
        raise SafetyViolationError(
            "include_empty_groups is not supported for count_groups queries."
        )
    if query.get("having_expression") is not None:
        raise SafetyViolationError(
            "include_empty_groups is not supported with HAVING."
        )
    if query.get("limit") is not None:
        raise SafetyViolationError(
            "include_empty_groups is not supported for user-limited or ranked queries."
        )
    if query.get("count_entity") is not None:
        raise SafetyViolationError(
            "include_empty_groups is not supported for entity-count plans."
        )

    fact_table = query.get("table")
    if not isinstance(fact_table, str) or fact_table not in valid_tables:
        raise SafetyViolationError("include_empty_groups requires a known fact table.")
    joins = query.get("joins")
    if not isinstance(joins, list) or not joins:
        raise SafetyViolationError(
            "include_empty_groups requires a verified join from facts to a grouping entity."
        )

    path_tables = [fact_table]
    previous = fact_table
    for join in joins:
        if not isinstance(join, dict):
            raise SafetyViolationError("Each join must be an object.")
        relationship = join.get("relationship")
        if (
            join.get("join_type") != "INNER"
            or not isinstance(relationship, dict)
            or relationship.get("table") != previous
            or relationship.get("cardinality") != "many_to_one"
            or join.get("table") != relationship.get("referenced_table")
            or join.get("table") in path_tables
        ):
            raise SafetyViolationError(
                "include_empty_groups requires one unbranched INNER join chain "
                "from the fact table through verified many-to-one parents."
            )
        previous = join["table"]
        path_tables.append(previous)
    grouping_table = path_tables[-1]
    if grouping_table == fact_table:
        raise SafetyViolationError(
            "include_empty_groups requires a grouping entity separate from the fact table."
        )

    primary_keys = [
        item.get("column")
        for item in schema_context.get(grouping_table, [])
        if isinstance(item, dict) and item.get("key") == "PRI"
    ]
    if len(primary_keys) != 1:
        raise SafetyViolationError(
            "include_empty_groups requires a single-column primary key on its grouping entity."
        )
    grouping_key = primary_keys[0]

    select_items = query.get("select_items")
    group_by = query.get("group_by")
    if (
        not isinstance(select_items, list)
        or not select_items
        or not isinstance(group_by, list)
        or not group_by
    ):
        raise SafetyViolationError(
            "include_empty_groups requires selected grouping columns and GROUP BY."
        )

    aliases: Set[str] = set()
    plain_items: List[Dict[str, Any]] = []
    aggregate_items: List[Dict[str, Any]] = []
    for item in select_items:
        if not isinstance(item, dict):
            raise SafetyViolationError("Each selected expression must be an object.")
        alias = _validate_identifier(item.get("alias"), kind="alias")
        if alias in aliases:
            raise SafetyViolationError("Selected aliases must be unique.")
        aliases.add(alias)

        aggregate = item.get("aggregate")
        owner = _resolve_column_table(item, path_tables, schema_context)
        if aggregate is None:
            if owner != grouping_table or item.get("column") == "*":
                raise SafetyViolationError(
                    "include_empty_groups may select plain columns only from its grouping entity."
                )
            plain_items.append(item)
        elif aggregate in {"SUM", "AVG", "MIN", "MAX"} and owner == fact_table:
            if item.get("distinct", False):
                raise SafetyViolationError(
                    "include_empty_groups does not support DISTINCT numeric aggregates."
                )
            aggregate_items.append(item)
        else:
            raise SafetyViolationError(
                "include_empty_groups supports only SUM, AVG, MIN, or MAX "
                "measured on the fact table."
            )
    if not plain_items or not aggregate_items:
        raise SafetyViolationError(
            "include_empty_groups requires a grouping column and a fact-table aggregate."
        )

    normalized_groups: List[Dict[str, str]] = []
    group_pairs: Set[Tuple[str, str]] = set()
    for group in group_by:
        if isinstance(group, str):
            group = {"table": fact_table, "column": group}
        if not isinstance(group, dict):
            raise SafetyViolationError("Each grouping expression must identify a column.")
        owner = _resolve_column_table(group, path_tables, schema_context)
        if owner != grouping_table:
            raise SafetyViolationError(
                "include_empty_groups may group by only one terminal grouping entity."
            )
        column = _validate_identifier(group.get("column"), kind="group column")
        pair = (owner, column)
        if pair in group_pairs:
            raise SafetyViolationError("GROUP BY must not contain duplicate columns.")
        group_pairs.add(pair)
        normalized_groups.append({"table": owner, "column": column})
    if (grouping_table, grouping_key) not in group_pairs:
        raise SafetyViolationError(
            "include_empty_groups must group by the grouping entity's primary key."
        )
    for item in plain_items:
        if (grouping_table, item["column"]) not in group_pairs:
            raise SafetyViolationError(
                "Every selected grouping column must appear in GROUP BY."
            )

    where_expression = query.get("where_expression")
    if where_expression is None and query.get("where_conditions"):
        legacy = query["where_conditions"]
        if not isinstance(legacy, list):
            raise SafetyViolationError("'where_conditions' must be a list.")
        if len(legacy) == 1:
            where_expression = legacy[0]
        elif legacy:
            combiner = query.get("combine_with", "AND")
            if combiner not in ALLOWED_COMBINERS:
                raise SafetyViolationError("Disallowed filter combiner.")
            where_expression = {"boolean": combiner, "conditions": legacy}
    outer_filters, inner_filters = _partition_empty_group_filters(
        where_expression,
        grouping_table,
        set(path_tables),
    )

    group_aliases = [
        f"_eg_group_{index}" for index in range(len(normalized_groups))
    ]
    marker_alias = "_eg_fact_marker"
    if marker_alias in aliases or set(group_aliases) & aliases:
        raise SafetyViolationError("A selected alias conflicts with an internal SQL alias.")
    key_index = next(
        index for index, group in enumerate(normalized_groups)
        if group["column"] == grouping_key
    )
    inner_select_items = [
        {
            "table": group["table"],
            "column": group["column"],
            "aggregate": None,
            "distinct": False,
            "alias": group_aliases[index],
        }
        for index, group in enumerate(normalized_groups)
    ]
    inner_select_items.extend(
        {
            **item,
            "round_digits": None,
        }
        for item in aggregate_items
    )
    fact_keys = [
        item.get("column")
        for item in schema_context.get(fact_table, [])
        if isinstance(item, dict) and item.get("key") == "PRI"
    ]
    if len(fact_keys) != 1:
        raise SafetyViolationError(
            "include_empty_groups requires a single-column primary key on its fact table."
        )
    inner_select_items.append({
        "table": fact_table,
        "column": fact_keys[0],
        "aggregate": "MAX",
        "distinct": False,
        "alias": marker_alias,
    })
    inner_query = {
        **query,
        "select_items": inner_select_items,
        "where_expression": inner_filters,
        "where_conditions": [],
        "group_by": normalized_groups,
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "include_empty_groups": False,
        "order_by": [],
    }
    inner_query.pop("limit", None)

    return {
        "fact_table": fact_table,
        "grouping_table": grouping_table,
        "grouping_key": grouping_key,
        "key_alias": group_aliases[key_index],
        "marker_alias": marker_alias,
        "group_by": normalized_groups,
        "plain_items": plain_items,
        "aggregate_items": aggregate_items,
        "select_items": select_items,
        "outer_filters": outer_filters,
        "inner_query": inner_query,
        "group_aliases": group_aliases,
    }


def _build_empty_groups_sql(
    query: Dict[str, Any],
    valid_tables: List[str],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]],
) -> Tuple[str, List[Any], str, int, str | None, List[Any]]:
    plan = _prepare_empty_group_plan(query, valid_tables, schema_context)
    inner_sql, inner_params, _, _, _, _ = _build_sql(
        plan["inner_query"],
        valid_tables,
        schema_context,
        relationships,
        _as_derived_query=True,
    )

    grouping_table = plan["grouping_table"]
    grouping_tables = [grouping_table]
    select_parts = [
        _empty_group_projection(item, plan, grouping_tables, schema_context)
        for item in plan["select_items"]
    ]

    terminal_join = next(
        (join for join in query["joins"] if join["table"] == grouping_table),
        None,
    )
    selected_relationship = (
        terminal_join.get("relationship") if terminal_join is not None else None
    )
    relationship = next(
        (
            candidate
            for candidate in relationships
            if isinstance(selected_relationship, dict)
            and _relationship_matches(candidate, selected_relationship)
        ),
        None,
    )
    if relationship is None:
        raise SafetyViolationError(
            "The grouping entity is not the terminal table in the verified path."
        )
    grouping_key_pair = next(
        (
            pair for pair in relationship.get("column_pairs", [])
            if pair.get("referenced_column") == plan["grouping_key"]
        ),
        None,
    )
    if grouping_key_pair is None:
        raise SafetyViolationError(
            "The verified terminal relationship does not reference the grouping primary key."
        )
    _validate_identifier(grouping_key_pair["column"], kind="join column")
    on_clause = (
        f"{_qualified(grouping_table, plan['grouping_key'])} = "
        f"{_qualified('_empty_group_aggregates', plan['key_alias'])}"
    )

    outer_where, outer_params = _build_filter_expression(
        plan["outer_filters"],
        grouping_tables,
        schema_context,
    )
    aliases = {
        item["alias"]
        for item in query["select_items"]
        if isinstance(item, dict) and item.get("alias")
    }
    order_by_clause = _build_order_by_clause(
        query.get("order_by", []),
        grouping_tables,
        schema_context,
        aliases,
    )
    tie_breaker = f"{_qualified(grouping_table, plan['grouping_key'])} ASC"
    if tie_breaker not in order_by_clause.split(", "):
        order_by_clause = ", ".join(
            part for part in (order_by_clause, tie_breaker) if part
        )

    limit = MAX_RESULT_LIMIT
    sql_parts = [
        f"SELECT /*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */ "
        f"{', '.join(select_parts)}",
        f"FROM {_quote(grouping_table)}",
        f"LEFT JOIN ({inner_sql}) AS {_quote('_empty_group_aggregates')} "
        f"ON {on_clause}",
    ]
    if outer_where:
        sql_parts.append(f"WHERE {outer_where}")
    if order_by_clause:
        sql_parts.append(f"ORDER BY {order_by_clause}")
    sql_parts.append("LIMIT %s")
    return (
        " ".join(sql_parts),
        list(inner_params) + list(outer_params) + [limit + 1],
        query["table"],
        limit,
        None,
        [],
    )


def _empty_group_projection(
    item: Dict[str, Any],
    plan: Dict[str, Any],
    grouping_tables: List[str],
    schema_context: Dict[str, Any],
) -> str:
    alias = _quote(_validate_identifier(item.get("alias"), kind="alias"))
    aggregate = item.get("aggregate")
    if aggregate is None:
        return _build_select_clause([item], grouping_tables, schema_context)

    aggregate_alias = f"{_quote('_empty_group_aggregates')}.{alias}"
    if aggregate == "SUM":
        expression = (
            f"CASE WHEN {_quote('_empty_group_aggregates')}."
            f"{_quote(plan['marker_alias'])} IS NULL THEN 0 "
            f"ELSE {aggregate_alias} END"
        )
    else:
        expression = aggregate_alias
    digits = item.get("round_digits")
    if digits is not None:
        if type(digits) is not int or not 0 <= digits <= 6:
            raise SafetyViolationError("round_digits must be from 0 to 6.")
        expression = f"ROUND({expression}, {digits})"
    return f"{expression} AS {alias}"


def _canonical_expression(expression: exp.Expression) -> str:
    return expression.sql(dialect="mysql")


def _expected_clause_expression(sql: str) -> exp.Expression:
    parsed = sqlglot.parse_one(sql.replace("%s", "0"), read="mysql")
    return parsed


def _verify_empty_groups_ast(
    sql: str,
    tree: exp.Select,
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
    expected_query: Dict[str, Any],
) -> None:
    plan = _prepare_empty_group_plan(
        expected_query,
        list(schema_context),
        schema_context,
    )
    from_clause = tree.args.get("from_")
    base = from_clause.this if from_clause is not None else None
    joins = tree.args.get("joins") or []
    if (
        not isinstance(base, exp.Table)
        or base.name != plan["grouping_table"]
        or base.args.get("alias") is not None
        or base.db
        or base.catalog
        or len(joins) != 1
        or tree.args.get("group") is not None
        or tree.args.get("having") is not None
        or tree.args.get("offset") is not None
    ):
        raise SafetyViolationError(
            "include_empty_groups SQL must select one grouping table and left-join one aggregate subquery."
        )

    join = joins[0]
    subquery = join.this
    if (
        not isinstance(subquery, exp.Subquery)
        or not isinstance(subquery.this, exp.Select)
        or subquery.alias != "_empty_group_aggregates"
        or join.args.get("side") != "LEFT"
        or join.args.get("kind") not in (None, "OUTER")
        or join.args.get("method") is not None
        or join.args.get("on") is None
    ):
        raise SafetyViolationError(
            "include_empty_groups requires one LEFT JOIN to its validated aggregate subquery."
        )

    key_condition = (
        f"{_qualified(plan['grouping_table'], plan['grouping_key'])} = "
        f"{_qualified('_empty_group_aggregates', plan['key_alias'])}"
    )
    expected_on = _expected_clause_expression(
        f"SELECT 1 WHERE {key_condition}"
    ).args["where"].this
    if _canonical_expression(join.args["on"]) != _canonical_expression(expected_on):
        raise SafetyViolationError(
            "include_empty_groups LEFT JOIN must match the grouping entity primary key."
        )

    expected_projections = [
        _expected_clause_expression(f"SELECT {projection}").expressions[0]
        for projection in (
            _empty_group_projection(item, plan, [plan["grouping_table"]], schema_context)
            for item in plan["select_items"]
        )
    ]
    if len(tree.expressions) != len(expected_projections) or any(
        _canonical_expression(actual) != _canonical_expression(expected)
        for actual, expected in zip(tree.expressions, expected_projections)
    ):
        raise SafetyViolationError(
            "include_empty_groups SELECT expressions do not match the validated query plan."
        )

    expected_where, _ = _build_filter_expression(
        plan["outer_filters"],
        [plan["grouping_table"]],
        schema_context,
    )
    actual_where = tree.args.get("where")
    if bool(expected_where) != bool(actual_where):
        raise SafetyViolationError(
            "include_empty_groups dimension filters do not match the validated query plan."
        )
    if expected_where:
        expected_expression = _expected_clause_expression(
            f"SELECT 1 WHERE {expected_where}"
        ).args["where"].this
        if _canonical_expression(actual_where.this) != _canonical_expression(
            expected_expression
        ):
            raise SafetyViolationError(
                "include_empty_groups dimension filters do not match the validated query plan."
            )

    aliases = {
        item.get("alias")
        for item in plan["select_items"]
        if isinstance(item, dict) and item.get("alias")
    }
    expected_order = _build_order_by_clause(
        expected_query.get("order_by", []),
        [plan["grouping_table"]],
        schema_context,
        aliases,
    )
    tie_breaker = _qualified(plan["grouping_table"], plan["grouping_key"]) + " ASC"
    if tie_breaker not in expected_order.split(", "):
        expected_order = ", ".join(
            part for part in (expected_order, tie_breaker) if part
        )
    actual_order = tree.args.get("order")
    if bool(expected_order) != bool(actual_order):
        raise SafetyViolationError(
            "include_empty_groups ordering does not match the validated query plan."
        )
    if expected_order:
        expected_order_clause = _expected_clause_expression(
            f"SELECT 1 ORDER BY {expected_order}"
        ).args["order"]
        if _canonical_expression(actual_order) != _canonical_expression(
            expected_order_clause
        ):
            raise SafetyViolationError(
                "include_empty_groups ordering does not match the validated query plan."
            )

    limit = tree.args.get("limit")
    limit_expression = limit.args.get("expression") if limit is not None else None
    if (
        not isinstance(limit_expression, exp.Literal)
        or not limit_expression.is_int
        or int(limit_expression.this) != 0
        or not re.search(r"\bLIMIT\s+%s\s*$", sql, re.IGNORECASE)
    ):
        raise SafetyViolationError(
            "include_empty_groups requires the configured bounded result limit."
        )

    _ast_verify(
        subquery.this.sql(dialect="mysql"),
        schema_context,
        relationships,
        None,
        expected_query=plan["inner_query"],
    )

def _catalog_relationship(join, relationships):
    """Use the verified catalog edge so the model's plan can't claim a flag."""
    claimed = join["relationship"]
    key = (
        claimed.get("constraint_name"),
        claimed.get("table"),
        claimed.get("referenced_table"),
    )
    for edge in relationships or []:
        if (
            edge.get("constraint_name"),
            edge.get("table"),
            edge.get("referenced_table"),
        ) == key:
            return edge
    return {k: v for k, v in claimed.items() if k != "single_row_when"}

def _build_sql(
    query: Dict[str, Any],
    valid_tables: List[str],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]],
    *,
    _as_derived_query: bool = False,
) -> Tuple[str, List[Any], str, int, str | None, List[Any]]:
    include_empty_groups = query.get("include_empty_groups", False)
    if not isinstance(include_empty_groups, bool):
        raise SafetyViolationError("'include_empty_groups' must be a boolean.")
    if include_empty_groups and query.get("search") is not None:
        raise SafetyViolationError(
            "The typed search operation cannot be combined with count_groups "
            "or include_empty_groups."
        )
    if include_empty_groups:
        if any(
            isinstance(item, dict)
            and ("period" in item or "period_comparison" in item)
            for item in query.get("select_items", [])
        ):
            raise SafetyViolationError(
                "Conditional periods cannot be combined with include_empty_groups."
            )
        if _as_derived_query:
            raise SafetyViolationError(
                "include_empty_groups cannot be nested in a derived query."
            )
        return _build_empty_groups_sql(
            query,
            valid_tables,
            schema_context,
            relationships,
        )

    table = query.get("table")
    if not isinstance(table, str) or table not in valid_tables:
        raise SafetyViolationError(f"Table '{table}' is not among the known schema tables.")
    safe_table = _validate_identifier(table, kind="table")
    count_groups = query.get("count_groups", False)
    if not isinstance(count_groups, bool):
        raise SafetyViolationError("'count_groups' must be a boolean.")
    if query.get("search") is not None and count_groups:
        raise SafetyViolationError(
            "The typed search operation cannot be combined with count_groups "
            "or include_empty_groups."
        )

    joins = query.get("joins", [])
    if not isinstance(joins, list) or len(joins) + 1 > MAX_QUERY_TABLES:
        raise SafetyViolationError(f"A query may reference at most {MAX_QUERY_TABLES} tables.")
    query_tables = [safe_table]
    join_parts = []
    matched_relationships = []
    join_params: List[Any] = []
    for join in joins:
        if not isinstance(join, dict):
            raise SafetyViolationError("Each join must be an object.")
        target = join.get("table")
        join_type = join.get("join_type")
        selected_relationship = join.get("relationship")
        if target not in valid_tables or target in query_tables:
            raise SafetyViolationError(f"Join introduces invalid or repeated table '{target}'.")
        if not isinstance(join_type, str) or join_type not in {"INNER", "LEFT"}:
            raise SafetyViolationError(f"Disallowed join type: {join_type!r}.")
        if not isinstance(selected_relationship, dict):
            raise SafetyViolationError("Join is missing its verified relationship.")
        relationship = next(
            (
                candidate for candidate in relationships
                if _relationship_matches(candidate, selected_relationship)
            ),
            None,
        )
        if relationship is None:
            raise SafetyViolationError("Join does not use a verified schema relationship.")
        matched_relationships.append(relationship)
        endpoints = {relationship["table"], relationship["referenced_table"]}
        if target not in endpoints or len(endpoints & set(query_tables)) != 1:
            raise SafetyViolationError(
                f"Join to '{target}' must connect exactly one existing query table."
            )
        if not relationship.get("column_pairs"):
            raise SafetyViolationError("Verified relationship has no column pairs.")
        predicates = []
        for pair in relationship["column_pairs"]:
            child_column = _validate_identifier(pair["column"], kind="join column")
            parent_column = _validate_identifier(pair["referenced_column"], kind="join column")
            predicates.append(
                f"{_qualified(relationship['table'], child_column)} = "
                f"{_qualified(relationship['referenced_table'], parent_column)}"
            )

        raw_conditions = join.get("conditions", [])
        if raw_conditions:
            if not isinstance(raw_conditions, list):
                raise SafetyViolationError("Join 'conditions' must be a list.")
            target_cols = {
                col["column"] if isinstance(col, dict) else col
                for col in schema_context.get(target, [])
            }
            for cond in raw_conditions:
                if not isinstance(cond, dict):
                    raise SafetyViolationError("Each join condition must be an object.")
                cond_table = cond.get("table")
                if cond_table is not None and cond_table != target:
                    raise SafetyViolationError(
                        f"Join condition table '{cond_table}' must match joined table '{target}'."
                    )
                col_name = cond.get("column")
                if not col_name or col_name not in target_cols:
                    raise SafetyViolationError(
                        f"Join condition column '{col_name}' does not belong to table '{target}'."
                    )
                safe_col = _validate_identifier(col_name, kind="join condition column")
                op = cond.get("operator")
                if op not in ALLOWED_OPERATORS:
                    raise SafetyViolationError(f"Disallowed join condition operator: {op!r}.")
                val = cond.get("value")
                col_sql = _qualified(target, safe_col)
                if op in {"IS NULL", "IS NOT NULL"}:
                    if "value" in cond:
                        raise SafetyViolationError(f"{op} must not include a value.")
                    predicates.append(f"{col_sql} {op}")
                elif op == "BETWEEN":
                    if not isinstance(val, list) or len(val) != 2 or any(isinstance(x, (dict, list)) for x in val):
                        raise SafetyViolationError("BETWEEN requires exactly two scalar values.")
                    predicates.append(f"{col_sql} BETWEEN %s AND %s")
                    join_params.extend(val)
                elif op in {"IN", "NOT IN"}:
                    if not isinstance(val, list) or not val or any(isinstance(x, (dict, list)) for x in val):
                        raise SafetyViolationError(f"{op} requires a non-empty list of scalars.")
                    placeholders = ", ".join(["%s"] * len(val))
                    predicates.append(f"{col_sql} {op} ({placeholders})")
                    join_params.extend(val)
                else:
                    if isinstance(val, (dict, list)):
                        raise SafetyViolationError(f"{op} requires a scalar value.")
                    predicates.append(f"{col_sql} {op} %s")
                    join_params.append(val)

        join_parts.append(
            f"{join_type} JOIN {_quote(target)} ON {' AND '.join(predicates)}"
        )
        query_tables.append(target)

    select_items = query.get("select_items", [])
    if not isinstance(select_items, list) or not select_items:
        raise SafetyViolationError("Query must contain at least one select item.")
    select_params: List[Any] = []
    if count_groups:
        if any(
            isinstance(item, dict)
            and ("period" in item or "period_comparison" in item)
            for item in select_items
        ):
            raise SafetyViolationError(
                "Conditional periods cannot be used with count_groups."
            )
        if len(select_items) != 1:
            raise SafetyViolationError("A count_groups query must select one COUNT(*) result.")
        item = select_items[0]
        if (
            not isinstance(item, dict)
            or item.get("column") != "*"
            or item.get("aggregate") != "COUNT"
            or item.get("distinct", False)
            or not isinstance(item.get("alias"), str)
        ):
            raise SafetyViolationError("A count_groups query must select COUNT(*) with an alias.")
        count_alias = _validate_identifier(item["alias"], kind="alias")
    else:
        _verify_metric_filter_units(query, schema_context, query_tables)
        _validate_date_bucket_plan(
            select_items,
            query.get("group_by", []),
        )
        select_clause = _build_select_clause(
            select_items, query_tables, schema_context, select_params
        )
        if not select_clause:
            raise SafetyViolationError(
                "Query has no select_items — refusing to run SELECT with nothing selected."
            )

    if len(query_tables) > 1:
        for item in select_items:
            if count_groups:
                continue
            if item.get("metric") == "share_of_rows":
                condition = item.get("metric_condition")
                if (
                    not isinstance(condition, dict)
                    or condition.get("table") != safe_table
                    or any(
                        _catalog_relationship(join, relationships).get(
                            "cardinality"
                        ) != "many_to_one"
                        for join in joins
                    )
                ):
                    raise SafetyViolationError(
                        "share_of_rows must measure base-table rows and may join only "
                        "to verified many-to-one parents."
                    )
                continue
            if item.get("aggregate") not in {"SUM", "AVG", "MIN", "MAX"}:
                continue
            owner = (
                validate_metric(
                    item["metric"], schema_context, query_tables
                ).table
                if item.get("metric") is not None
                else _resolve_column_table(item, query_tables, schema_context)
            )
            if owner != safe_table or any(
                not join_is_fanout_safe(
                    join["table"],
                    _catalog_relationship(join, relationships),
                    query.get("where_expression"),
                    join.get("conditions"),
                )
                for join in joins
            ):
                raise SafetyViolationError(
                    "Joined numeric aggregates must measure the base table and join only to its many-to-one parents.",
                    rule_id="fanout_select_aggregate",
                )

    where_clause, where_params = _build_query_where_clause(
        query,
        query_tables,
        schema_context,
        relationships,
    )
    group_by_clause = _build_group_by_clause(
        query.get("group_by", []), query_tables, schema_context
    )
    having_expression = query.get("having_expression")
    if having_expression is not None and not group_by_clause:
        raise SafetyViolationError("HAVING requires a grouped query.")
    having_clause, having_params = _build_filter_expression(
        having_expression,
        query_tables,
        schema_context,
        aggregate_filters=True,
    )
    for leaf in _flatten_filter_expression(having_expression):
        if leaf.get("aggregate") in {"SUM", "AVG", "MIN", "MAX"}:
            owner = _resolve_column_table(leaf, query_tables, schema_context)
            if len(query_tables) > 1 and (
                owner != safe_table
                or any(
                    not join_is_fanout_safe(
                        join["table"],
                        _catalog_relationship(join, relationships),
                        query.get("where_expression"),
                        join.get("conditions"),
                    )
                    for join in joins
                )
            ):
                raise SafetyViolationError(
                    "HAVING numeric aggregates must measure the base table and join only to its many-to-one parents.",
                    rule_id="fanout_having_aggregate",
                )
    if count_groups and (not group_by_clause or not having_clause):
        raise SafetyViolationError(
            "count_groups requires GROUP BY and a HAVING expression."
        )
    if count_groups and query.get("order_by"):
        raise SafetyViolationError("count_groups does not accept an outer ordering.")
    group_columns = {
        (
            _resolve_column_table(group if isinstance(group, dict) else {"column": group}, query_tables, schema_context),
            group["column"] if isinstance(group, dict) else group,
        )
        for group in query.get("group_by", [])
    }
    orderable_group_columns = {
        (
            _resolve_column_table(
                group if isinstance(group, dict) else {"column": group},
                query_tables,
                schema_context,
            ),
            group["column"] if isinstance(group, dict) else group,
        )
        for group in query.get("group_by", [])
        if isinstance(group, str)
        or (isinstance(group, dict) and group.get("date_bucket") is None)
    }
    grouped_date_buckets = {
        (
            _resolve_column_table(group, query_tables, schema_context),
            group["column"],
            group.get("date_bucket"),
        )
        for group in query.get("group_by", [])
        if isinstance(group, dict) and group.get("date_bucket") is not None
    }
    if any(_is_aggregate_select_item(item) for item in select_items):
        for item in select_items:
            if _is_aggregate_select_item(item):
                continue
            if item.get("metric") is not None:
                raise SafetyViolationError(
                    "A plain calculated metric cannot appear in a grouped aggregate query."
                )
            selected_column = (
                _resolve_column_table(item, query_tables, schema_context),
                item["column"],
            )
            if item.get("date_bucket") is not None:
                selected_is_grouped = (
                    *selected_column,
                    item["date_bucket"],
                ) in grouped_date_buckets
            else:
                selected_is_grouped = selected_column in group_columns
            if not selected_is_grouped:
                raise SafetyViolationError(
                    "Every plain selected column in an aggregate query must appear in GROUP BY.",
                    rule_id="group_by_selected_column",
                )
    aliases = {item.get("alias") for item in select_items if item.get("alias")}
    order_by_clause = "" if count_groups or _as_derived_query else _build_order_by_clause(
        query.get("order_by", []),
        query_tables,
        schema_context,
        aliases,
        select_items,
        query.get("group_by", []),
    )
    if (
        query.get("limit") is not None
        and not any(_is_aggregate_select_item(item) for item in select_items)
    ):
        selected_plain_columns = {
            (
                _resolve_column_table(item, query_tables, schema_context),
                item["column"],
            )
            for item in select_items
            if (
                item.get("aggregate") is None
                and item.get("metric") is None
                and item.get("column") != "*"
            )
        }
        for order in query.get("order_by", []):
            if order["column"] in aliases:
                continue
            ordered_column = (
                _resolve_column_table(order, query_tables, schema_context),
                order["column"],
            )
            if ordered_column in selected_plain_columns:
                continue
            for join, relationship in zip(joins, matched_relationships):
                if (
                    join["table"] == ordered_column[0]
                    and relationship["table"] == join["table"]
                    and relationship.get("cardinality") == "many_to_one"
                ):
                    raise SafetyViolationError(
                        "A limited row query cannot rank parent rows by an unselected "
                        "column from a one-to-many child table.",
                        rule_id="order_by_hidden_child_column",
                    )
    if not count_groups and (
        group_by_clause
        or any(_is_aggregate_select_item(item) for item in select_items)
    ):
        for order in query.get("order_by", []):
            if order["column"] in aliases:
                continue
            ordered_column = (
                _resolve_column_table(order, query_tables, schema_context),
                order["column"],
            )
            if ordered_column not in orderable_group_columns:
                import logging as _dbg_logging
                _dbg_logging.getLogger(__name__).warning(
                    "TEMP order_by_not_grouped: order=%r aliases=%r "
                    "orderable_group_columns=%r group_by=%r select_items=%r",
                    order, sorted(aliases), sorted(orderable_group_columns),
                    query.get("group_by"), query.get("select_items"),
                )
                raise SafetyViolationError(
                    "ORDER BY in a grouped or aggregate query must use a GROUP BY column or a selected alias.",
                    rule_id="order_by_not_grouped",
                )
    if (
        not _as_derived_query
        and group_by_clause
        and any(_is_aggregate_select_item(item) for item in select_items)
    ):
        tie_breakers = []
        for group in query.get("group_by", []):
            if isinstance(group, str):
                group = {"column": group}
            column = _validate_identifier(group["column"], kind="column")
            owner = _resolve_column_table(group, query_tables, schema_context)
            date_bucket = group.get("date_bucket")
            if date_bucket is not None:
                tie_breakers.append(
                    f"{_date_bucket_expression(owner, column, date_bucket, schema_context)} ASC"
                )
                continue
            companion_date = None
            if column.lower().endswith("_month"):
                prefix = column[:-len("_month")]
                companion_date = next(
                    (
                        candidate
                        for candidate in (f"{prefix}_date", f"{prefix}_at")
                        if any(
                            isinstance(entry, dict)
                            and entry.get("column") == candidate
                            and str(entry.get("data_type", "")).lower()
                            in {"date", "datetime", "timestamp"}
                            for entry in schema_context.get(owner, [])
                        )
                    ),
                    None,
                )
            if companion_date is not None:
                tie_breaker = f"MIN({_qualified(owner, companion_date)}) ASC"
                if tie_breaker not in order_by_clause.split(", "):
                    tie_breakers.append(tie_breaker)
            else:
                tie_breakers.append(f"{_qualified(owner, column)} ASC")
        order_by_clause = ", ".join(
            part for part in (order_by_clause, *tie_breakers) if part
        )

    requested_limit = query.get("limit")
    if requested_limit is not None and (
        type(requested_limit) is not int
        or not 1 <= requested_limit <= MAX_RESULT_LIMIT
    ):
        raise SafetyViolationError(
            f"Query limit must be an integer from 1 to {MAX_RESULT_LIMIT}."
        )
    limit = 1 if count_groups else (
        requested_limit
        if requested_limit is not None
        else DEFAULT_LIMIT if not group_by_clause else MAX_RESULT_LIMIT
    )

    if count_groups:
        group_items = query.get("group_by", [])
        inner_columns = []
        for index, group in enumerate(group_items):
            if isinstance(group, str):
                group = {"column": group}
            owner = _resolve_column_table(group, query_tables, schema_context)
            column = _validate_identifier(group["column"], kind="group column")
            inner_columns.append(
                f"{_qualified(owner, column)} AS {_quote(f'_group_{index}')}"
            )
        inner_parts = [
            f"SELECT {', '.join(inner_columns)}",
            f"FROM {_quote(safe_table)}",
            *join_parts,
        ]
        if where_clause:
            inner_parts.append(f"WHERE {where_clause}")
        inner_parts.append(f"GROUP BY {group_by_clause}")
        inner_parts.append(f"HAVING {having_clause}")
        inner_sql = " ".join(inner_parts)
        sql = (
            f"SELECT /*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */ "
            f"COUNT(*) AS {_quote(count_alias)} "
            f"FROM ({inner_sql}) AS {_quote('_qualified_groups')} LIMIT %s"
        )
        params = join_params + where_params + having_params + [limit + 1]
    else:
        sql_parts = [
            f"SELECT /*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */ {select_clause}",
            f"FROM {_quote(safe_table)}",
        ]
        sql_parts.extend(join_parts)
        if where_clause:
            sql_parts.append(f"WHERE {where_clause}")
        if group_by_clause:
            sql_parts.append(f"GROUP BY {group_by_clause}")
        if having_clause:
            sql_parts.append(f"HAVING {having_clause}")
        if order_by_clause:
            sql_parts.append(f"ORDER BY {order_by_clause}")
        if not _as_derived_query:
            sql_parts.append("LIMIT %s")
        sql = " ".join(sql_parts)
        params = select_params + join_params + where_params + having_params
        if not _as_derived_query:
            params.append(limit + 1)
    count_sql = None
    count_params: List[Any] = []
    has_scalar_aggregate = (
        not count_groups
        and not group_by_clause
        and not having_clause
        and bool(select_items)
        and all(
            isinstance(item, dict)
            and item.get("aggregate") in {"COUNT", "SUM", "AVG", "MIN", "MAX"}
            for item in select_items
        )
    )
    if has_scalar_aggregate:
        count_parts = [
            f"SELECT /*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */ COUNT(*)",
            f"FROM {_quote(safe_table)}",
            *join_parts,
        ]
        if where_clause:
            count_parts.append(f"WHERE {where_clause}")
        count_sql = " ".join(count_parts)
        count_params = list(join_params) + list(where_params)

    return sql, params, safe_table, limit, count_sql, count_params


def _flatten_filter_expression(
    expression: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    if expression is None:
        return []
    if "boolean" in expression:
        return [
            leaf
            for condition in expression.get("conditions", [])
            for leaf in _flatten_filter_expression(condition)
        ]
    return [expression]


# ---------------------------------------------------------------------------
# LAYER 2: AST verification (the new, real "Change 1" work)
# ---------------------------------------------------------------------------
def _flatten_and(expression: exp.Expression) -> List[exp.Expression]:
    if isinstance(expression, exp.And):
        return _flatten_and(expression.this) + _flatten_and(expression.expression)
    return [expression]

def _personal_column_use_allowed(col_node: exp.Column) -> bool:
    """A personal column is only safe inside COUNT(...) / COUNT(DISTINCT ...) or an IS [NOT] NULL test."""
    parent = col_node.parent
    if isinstance(parent, exp.Distinct):
        parent = parent.parent
    if isinstance(parent, exp.Count):
        return True
    return isinstance(col_node.parent, exp.Is)

def _ast_verify(
    sql: str,
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None = None,
    count_entity: Dict[str, str] | None = None,
    expected_query: Dict[str, Any] | None = None,
) -> None:
    """
    Parses the already-built SQL and independently re-checks it structurally.
    """
    # sqlglot has no concept of the '%s' driver placeholder (that's a
    # pymysql/aiomysql convention, not real SQL) — it reads '%' as modulo
    # and fails to parse. Swap in a harmless literal for parsing purposes
    # only; this function never executes SQL, it only checks structure.
    # Safe because '%s' never appears inside backtick-quoted identifiers.
    sql_for_parsing = sql.replace("%s", "0")

    try:
        parsed = sqlglot.parse(sql_for_parsing, read="mysql")
    except Exception as e:
        raise SafetyViolationError(f"SQL failed to parse — rejected: {e}") from e

    statements = [s for s in parsed if s is not None]
    if len(statements) != 1:
        raise SafetyViolationError(
            f"Expected exactly one SQL statement, found {len(statements)}. Rejected as unsafe."
        )

    tree = statements[0]

    if not isinstance(tree, exp.Select):
        raise SafetyViolationError(
            f"Root statement is not a SELECT (got {type(tree).__name__}). Rejected."
        )

    blocked_tables = find_blocked_tables(
        table.name for table in tree.find_all(exp.Table)
    )
    if blocked_tables:
        raise SafetyViolationError(
            f"Table(s) not allowed: {', '.join(blocked_tables)}",
            rule_id="blocked_table",
        )

    if (
        expected_query is not None
        and not expected_query.get("count_groups")
        and not expected_query.get("include_empty_groups")
    ):
        if bool(tree.args.get("group")) != bool(expected_query.get("group_by")):
            raise SafetyViolationError("SQL GROUP BY does not match the validated query plan.")
        if bool(tree.args.get("having")) != bool(expected_query.get("having_expression")):
            raise SafetyViolationError("SQL HAVING does not match the validated query plan.")

    for selected in tree.expressions:
        expression = selected.this if isinstance(selected, exp.Alias) else selected
        if isinstance(expression, exp.Star) or (
            isinstance(expression, exp.Column) and expression.name == "*"
        ):
            if any(
                str(column.get("column", "")).lower() in SENSITIVE_RESULT_COLUMNS
                for source_table in tree.find_all(exp.Table)
                for column in schema_context.get(source_table.name, [])
                if isinstance(column, dict)
            ):
                raise SafetyViolationError(
                    "Wildcard SQL could expose sensitive credential columns."
                )

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_AST_TYPES):
            raise SafetyViolationError(
                f"Forbidden SQL construct found in query: {type(node).__name__}. Rejected."
            )

    if expected_query is not None and expected_query.get("include_empty_groups"):
        _verify_empty_groups_ast(
            sql,
            tree,
            schema_context,
            relationships,
            expected_query,
        )
        return

    from_clause = tree.args.get("from_")
    base = from_clause.this if from_clause is not None else None
    if isinstance(base, exp.Subquery):
        if expected_query is None or not expected_query.get("count_groups"):
            raise SafetyViolationError(
                "Derived-table queries are allowed only for validated count_groups plans."
            )
        projections = tree.expressions
        if (
            len(projections) != 1
            or not isinstance(projections[0], exp.Alias)
            or not isinstance(projections[0].this, exp.Count)
            or not isinstance(projections[0].this.this, exp.Star)
            or not isinstance(base.this, exp.Select)
            or base.alias != "_qualified_groups"
            or tree.args.get("joins")
            or tree.args.get("where")
            or tree.args.get("group")
            or tree.args.get("having")
            or tree.args.get("order")
        ):
            raise SafetyViolationError(
                "count_groups must be a scalar COUNT(*) over one grouped subquery."
            )
        expected_items = expected_query.get("select_items", [])
        if (
            len(expected_items) != 1
            or projections[0].alias != expected_items[0].get("alias")
        ):
            raise SafetyViolationError(
                "count_groups output alias does not match its validated query plan."
            )
        if tree.args.get("limit") is None:
            raise SafetyViolationError("count_groups must have a bounded outer LIMIT.")
        inner_plan = dict(expected_query)
        inner_plan["count_groups"] = False
        inner_plan["select_items"] = [
            {
                "table": group.get("table"),
                "column": group.get("column"),
                "aggregate": None,
                "alias": f"_group_{index}",
            }
            for index, group in enumerate(expected_query.get("group_by", []))
        ]
        inner_plan["group_by"] = expected_query.get("group_by", [])
        inner_plan["order_by"] = []
        self_sql = base.this.sql(dialect="mysql")
        _ast_verify(
            self_sql,
            schema_context,
            relationships,
            None,
            expected_query=inner_plan,
        )
        return
    if isinstance(base, exp.Select):
        raise SafetyViolationError("Nested SELECT must be a validated count_groups subquery.")
    if not isinstance(base, exp.Table):
        raise SafetyViolationError("SELECT must have exactly one base table.")
    if base.args.get("alias") is not None or base.db or base.catalog:
        raise SafetyViolationError("Table aliases and cross-database references are not allowed.")

    table_order = [base.name]
    joins = tree.args.get("joins") or []
    if len(joins) + 1 > MAX_QUERY_TABLES:
        raise SafetyViolationError(f"A query may reference at most {MAX_QUERY_TABLES} tables.")
    verified_relationships = relationships or []
    matched_relationships = []
    for join in joins:
        target = join.this
        if (
            not isinstance(target, exp.Table)
            or target.args.get("alias") is not None
            or target.db
            or target.catalog
            or target.name in table_order
        ):
            raise SafetyViolationError("JOIN must add one known, non-aliased table.")
        if (
            join.args.get("kind") not in (None, "INNER")
            or join.args.get("method") is not None
            or join.args.get("side") not in (None, "LEFT")
        ):
            raise SafetyViolationError("Only INNER and LEFT joins are allowed.")
        on_clause = join.args.get("on")
        if on_clause is None:
            raise SafetyViolationError("Every JOIN must have an ON clause.")

        actual_pairs = set()
        for predicate in _flatten_and(on_clause):
            if (
                isinstance(predicate, exp.EQ)
                and isinstance(predicate.this, exp.Column)
                and isinstance(predicate.expression, exp.Column)
                and predicate.this.table
                and predicate.expression.table
                and predicate.this.table != predicate.expression.table
            ):
                left, right = predicate.this, predicate.expression
                actual_pairs.add(frozenset({(left.table, left.name), (right.table, right.name)}))
            else:
                cols = list(predicate.find_all(exp.Column))
                if not cols:
                    raise SafetyViolationError(
                        "JOIN ON clauses may contain only verified key equalities or column predicates."
                    )
                target_schema_cols = {
                    c["column"] if isinstance(c, dict) else c
                    for c in schema_context.get(target.name, [])
                }
                for col in cols:
                    if col.table and col.table != target.name:
                        raise SafetyViolationError(
                            f"JOIN ON condition references table '{col.table}', only joined table '{target.name}' is allowed."
                        )
                    if col.name not in target_schema_cols:
                        raise SafetyViolationError(
                            f"JOIN ON condition references unknown column '{col.name}' for table '{target.name}'."
                        )

        matching_relationship = None
        for relationship in verified_relationships:
            endpoints = {relationship.get("table"), relationship.get("referenced_table")}
            if target.name not in endpoints or len(endpoints & set(table_order)) != 1:
                continue
            expected_pairs = {
                frozenset({
                    (relationship["table"], pair["column"]),
                    (relationship["referenced_table"], pair["referenced_column"]),
                })
                for pair in relationship.get("column_pairs", [])
            }
            if expected_pairs and actual_pairs == expected_pairs:
                matching_relationship = relationship
                break
        if matching_relationship is None:
            raise SafetyViolationError(
                f"JOIN to '{target.name}' does not exactly match a verified foreign-key relationship."
            )
        matched_relationships.append(matching_relationship)
        table_order.append(target.name)

    if expected_query is not None:
        for item in expected_query.get("select_items", []):
            if not isinstance(item, dict) or not (
                "period" in item or "period_comparison" in item
            ):
                continue
            expected_projection_sql = _build_select_clause(
                [item], table_order, schema_context
            )
            expected_projection = _expected_clause_expression(
                f"SELECT {expected_projection_sql}"
            ).expressions[0]
            actual_projection = next(
                (
                    projection
                    for projection in tree.expressions
                    if isinstance(projection, exp.Alias)
                    and projection.alias == item.get("alias")
                ),
                None,
            )
            if (
                actual_projection is None
                or _canonical_expression(actual_projection)
                != _canonical_expression(expected_projection)
            ):
                raise SafetyViolationError(
                    "Conditional aggregate SQL does not match its validated query plan."
                )

    expected_search = (
        expected_query.get("search")
        if isinstance(expected_query, dict)
        else None
    )
    if expected_search is None and (
        next(tree.find_all(exp.Exists), None) is not None
        or next(tree.find_all(exp.Subquery), None) is not None
    ):
        raise SafetyViolationError(
            "Subqueries are allowed only when generated by the typed search compiler."
        )
    if expected_query is not None:
        expected_where, _ = _build_query_where_clause(
            expected_query,
            table_order,
            schema_context,
            verified_relationships,
        )
        actual_where = tree.args.get("where")
        if bool(expected_where) != bool(actual_where):
            raise SafetyViolationError(
                "SQL WHERE does not match the validated query plan."
            )
        if expected_where:
            expected_where_expression = _expected_clause_expression(
                f"SELECT 1 WHERE {expected_where}"
            ).args["where"].this
            if _canonical_expression(actual_where.this) != _canonical_expression(
                expected_where_expression
            ):
                raise SafetyViolationError(
                    "SQL WHERE does not match the validated query plan."
                )

    if joins:
        selected_counts = [
            count_expression
            for selected in tree.expressions
            for count_expression in (
                (selected.this if isinstance(selected, exp.Alias) else selected)
                .find_all(exp.Count)
            )
        ]
        for count_expression in selected_counts:
            if not isinstance(count_expression.this, exp.Distinct):
                verified_share_denominator = False
                if expected_query is not None:
                    for item in expected_query.get("select_items", []):
                        if (
                            not isinstance(item, dict)
                            or item.get("metric") != "share_of_rows"
                        ):
                            continue
                        try:
                            share_sql = compile_share_of_rows(
                                item.get("metric_condition"),
                                schema_context,
                                table_order,
                            )
                        except ValueError as exc:
                            raise SafetyViolationError(str(exc)) from exc
                        expected_share_count = _expected_clause_expression(
                            f"SELECT {share_sql}"
                        ).find(exp.Count)
                        if (
                            expected_share_count is not None
                            and _canonical_expression(count_expression)
                            == _canonical_expression(expected_share_count)
                            and item["metric_condition"].get("table") == base.name
                            and all(
                                relationship.get("cardinality") == "many_to_one"
                                for relationship in matched_relationships
                            )
                        ):
                            verified_share_denominator = True
                            break
                if verified_share_denominator:
                    continue
                raise SafetyViolationError(
                    "COUNT in a multi-table query must count DISTINCT entity keys."
                )
            distinct_columns = count_expression.this.expressions
            if (
                len(distinct_columns) != 1
            ):
                raise SafetyViolationError(
                    "Joined COUNT must count one primary-key expression."
                )
            if isinstance(distinct_columns[0], exp.Case) and expected_query is not None:
                conditional_count_matches = False
                for item in expected_query.get("select_items", []):
                    if (
                        not isinstance(item, dict)
                        or item.get("aggregate") != "COUNT"
                        or item.get("aggregate_condition") is None
                    ):
                        continue
                    try:
                        expected_sql = _build_select_clause(
                            [item], table_order, schema_context, []
                        )
                    except SafetyViolationError:
                        continue
                    expected_count = _expected_clause_expression(
                        f"SELECT {expected_sql}"
                    ).find(exp.Count)
                    table = item.get("table")
                    column = item.get("column")
                    primary_keys = [
                        entry["column"]
                        for entry in schema_context.get(table, [])
                        if isinstance(entry, dict) and entry.get("key") == "PRI"
                    ]
                    if (
                        expected_count is not None
                        and _canonical_expression(count_expression)
                        == _canonical_expression(expected_count)
                        and len(primary_keys) == 1
                        and column == primary_keys[0]
                        and count_entity == {"table": table, "column": column}
                    ):
                        conditional_count_matches = True
                        break
                if conditional_count_matches:
                    continue
                raise SafetyViolationError(
                    "Conditional joined COUNT must match a validated primary-key plan."
                )
            if (
                not isinstance(distinct_columns[0], exp.Column)
                or not distinct_columns[0].table
            ):
                raise SafetyViolationError(
                    "Joined COUNT must count one table-qualified primary-key column."
                )
            counted_column = distinct_columns[0]
            primary_keys = [
                entry["column"]
                for entry in schema_context.get(counted_column.table, [])
                if isinstance(entry, dict) and entry.get("key") == "PRI"
            ]
            is_primary_key = (
                len(primary_keys) == 1 and counted_column.name == primary_keys[0]
            )
            # DISTINCT of a non-key column (for example distinct cities) cannot be
            # inflated by joins. A primary-key count must still match count_entity.
            if is_primary_key and count_entity != {
                "table": counted_column.table,
                "column": counted_column.name,
            }:
                raise SafetyViolationError(
                    "Joined COUNT must match the validated count_entity primary key."
                )
        
        having = tree.args.get("having")
        if having is not None:
            for count_expression in having.find_all(exp.Count):
                counted_expression = count_expression.this
                if isinstance(counted_expression, exp.Distinct):
                    columns = counted_expression.expressions
                    counted_expression = columns[0] if len(columns) == 1 else None
                if (
                    not isinstance(counted_expression, exp.Column)
                    or not counted_expression.table
                ):
                    raise SafetyViolationError(
                        "HAVING COUNT must target a table-qualified primary-key column."
                    )
                primary_keys = [
                    entry["column"]
                    for entry in schema_context.get(counted_expression.table, [])
                    if isinstance(entry, dict) and entry.get("key") == "PRI"
                ]
                if (
                    len(primary_keys) != 1
                    or counted_expression.name != primary_keys[0]
                ):
                    raise SafetyViolationError(
                        "HAVING COUNT must target the counted table's single-column primary key."
                    )

                parent_by_child = {}
                for relationship in matched_relationships:
                    child = relationship.get("table")
                    parent = relationship.get("referenced_table")
                    if (
                        relationship.get("cardinality") != "many_to_one"
                        or child in parent_by_child
                        or not isinstance(child, str)
                        or not isinstance(parent, str)
                    ):
                        raise SafetyViolationError(
                            "HAVING COUNT requires a verified many-to-one join chain."
                        )
                    parent_by_child[child] = parent

                path_tables = {counted_expression.table}
                current_table = counted_expression.table
                while current_table in parent_by_child:
                    current_table = parent_by_child[current_table]
                    if current_table in path_tables:
                        raise SafetyViolationError(
                            "HAVING COUNT join relationships contain a cycle."
                        )
                    path_tables.add(current_table)
                group_tables = {
                    column.table
                    for column in tree.args.get("group").expressions
                    if isinstance(column, exp.Column) and column.table
                } if tree.args.get("group") is not None else set()
                if path_tables != set(table_order) or not group_tables.issubset(path_tables):
                    raise SafetyViolationError(
                        "HAVING COUNT requires a single many-to-one chain from the counted "
                        "rows through every joined table and grouping dimension."
                    )
        for aggregate in (
            *tree.find_all(exp.Sum),
            *tree.find_all(exp.Avg),
            *tree.find_all(exp.Min),
            *tree.find_all(exp.Max),
        ):
            expression = aggregate.this
            metric_aggregate_matches = False
            share_aggregate_matches = False
            if expected_query is not None:
                aggregate_name = type(aggregate).__name__.upper()
                for item in expected_query.get("select_items", []):
                    metric_name = item.get("metric") if isinstance(item, dict) else None
                    if metric_name == "share_of_rows" and aggregate_name == "SUM":
                        try:
                            share_sql = compile_share_of_rows(
                                item.get("metric_condition"),
                                schema_context,
                                table_order,
                            )
                        except ValueError as exc:
                            raise SafetyViolationError(str(exc)) from exc
                        expected_share = _expected_clause_expression(
                            f"SELECT {share_sql}"
                        ).expressions[0]
                        expected_sum = expected_share.find(exp.Sum)
                        if (
                            expected_sum is not None
                            and _canonical_expression(aggregate)
                            == _canonical_expression(expected_sum)
                        ):
                            if (
                                item["metric_condition"].get("table") != base.name
                                or any(
                                    relationship.get("cardinality")
                                    != "many_to_one"
                                    for relationship in matched_relationships
                                )
                            ):
                                raise SafetyViolationError(
                                    "share_of_rows must measure base-table rows and "
                                    "join only to verified many-to-one parents."
                                )
                            metric_aggregate_matches = True
                            share_aggregate_matches = True
                            break
                    if (
                        metric_name is None
                        or metric_name == "share_of_rows"
                        or item.get("aggregate") != aggregate_name
                    ):
                        continue
                    try:
                        metric_sql = compile_metric(
                            metric_name, schema_context, table_order
                        )
                    except ValueError as exc:
                        raise SafetyViolationError(str(exc)) from exc
                    expected_aggregate = _expected_clause_expression(
                        f"SELECT {aggregate_name}({metric_sql})"
                    ).expressions[0]
                    if _canonical_expression(aggregate) == _canonical_expression(
                        expected_aggregate
                    ):
                        metric_aggregate_matches = True
                        break
            if metric_aggregate_matches:
                if share_aggregate_matches:
                    if any(
                        relationship.get("cardinality") != "many_to_one"
                        for relationship in matched_relationships
                    ):
                        raise SafetyViolationError(
                            "share_of_rows may join only to verified many-to-one parents."
                        )
                elif base.name != "offer" or any(
                    join.this.name != relationship["referenced_table"]
                    or relationship.get("cardinality") != "many_to_one"
                    for join, relationship in zip(joins, matched_relationships)
                ):
                    raise SafetyViolationError(
                        "Calculated numeric aggregates must measure the offer base "
                        "table and join only to verified many-to-one parents.",
                        rule_id="fanout_ast_aggregate",
                    )
                continue
            plan_where = (
                expected_query.get("where_expression")
                if isinstance(expected_query, dict)
                else None
            )
            plan_joins = (
                expected_query.get("joins", [])
                if isinstance(expected_query, dict)
                else []
            )
            if (
                not isinstance(expression, exp.Column)
                or expression.table != base.name
                or any(
                    not join_is_fanout_safe(
                        join.this.name,
                        relationship,
                        plan_where,
                        plan_join.get("conditions"),
                    )
                    for join, relationship, plan_join in zip(
                        joins, matched_relationships, plan_joins
                    )
                )
            ):
                raise SafetyViolationError(
                    "Joined numeric aggregates must measure the base table and join only to its many-to-one parents.",
                    rule_id="fanout_ast_aggregate",
                )

    aggregate_types = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
    has_aggregate = any(tree.find(aggregate_type) is not None for aggregate_type in aggregate_types)
    if has_aggregate:
        group_clause = tree.args.get("group")
        grouped_columns = {
            (column.table, column.name)
            for group_expression in (group_clause.expressions if group_clause else [])
            for column in group_expression.find_all(exp.Column)
        }
        for selected_expression in tree.expressions:
            expression = (
                selected_expression.this
                if isinstance(selected_expression, exp.Alias)
                else selected_expression
            )
            if any(expression.find(aggregate_type) for aggregate_type in aggregate_types):
                continue
            for column in expression.find_all(exp.Column):
                if column.name != "*" and (column.table, column.name) not in grouped_columns:
                    raise SafetyViolationError(
                        "Every plain selected column in an aggregate query must appear in GROUP BY.",
                        rule_id="group_by_selected_column",
                    )

    referenced_tables: Set[str] = {t.name for t in tree.find_all(exp.Table)}
    allowed_tables = set(table_order)
    if isinstance(expected_search, dict):
        scope = expected_search.get("scope")
        if isinstance(scope, str) and scope in SEARCHABLE_COLUMNS:
            allowed_tables.update(
                field["table"]
                for field in SEARCHABLE_COLUMNS[scope]
                if field.get("relationship") is not None
            )
    if (
        not set(table_order).issubset(referenced_tables)
        or not referenced_tables.issubset(allowed_tables)
        or (
            expected_search is None
            and referenced_tables != set(table_order)
        )
    ):
        raise SafetyViolationError("Query contains an unjoined or unexpected table reference.")
    for tbl in referenced_tables:
        if tbl not in schema_context:
            raise SafetyViolationError(
                f"AST references table '{tbl}' which is not in the known schema. Rejected."
            )

    aliases = {
        item.alias
        for item in tree.expressions
        if isinstance(item, exp.Alias) and item.alias
    }
    for col_node in tree.find_all(exp.Column):
        col_name = col_node.name
        if col_name == "*":
            continue
        if col_name.lower() in SENSITIVE_RESULT_COLUMNS:
            raise SafetyViolationError(
                f"SQL references sensitive credential column '{col_name}'."
            )
        owner_tables = (
            {col_node.table}
            if col_node.table
            else set(referenced_tables)
        )
        if any(
            (owner, col_name.lower()) in PERSONAL_RESULT_COLUMNS
            for owner in owner_tables
        ) and not _personal_column_use_allowed(col_node):
            raise SafetyViolationError(
                f"Personal column '{col_name}' can only be counted or tested "
                "for NULL; it cannot be shown, grouped, sorted or compared.",
                rule_id="personal_column_use",
            )
        table_name = col_node.table
        if table_name:
            if table_name not in referenced_tables:
                raise SafetyViolationError(
                    f"AST column '{table_name}.{col_name}' references a table outside the query."
                )
            known_columns = {
                col_info["column"]
                for col_info in schema_context.get(table_name, [])
                if isinstance(col_info, dict) and "column" in col_info
            }
            if col_name in known_columns:
                continue
        elif len(referenced_tables) == 1:
            only_table = next(iter(referenced_tables))
            known_columns = {
                col_info["column"]
                for col_info in schema_context.get(only_table, [])
                if isinstance(col_info, dict) and "column" in col_info
            }
            if col_name in known_columns or col_name in aliases:
                continue
        elif col_name in aliases:
            continue
        raise SafetyViolationError(
            f"AST references unknown or ambiguous column '{col_name}' "
            f"for table(s) {referenced_tables}. Rejected."
        )
# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_safety_check(
    query: Dict[str, Any],
    valid_tables: List[str],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """
    Main entry point for Node 7.

    NOTE: signature changed — now requires schema_context (Node 3's cached
    schema, table -> [{"column": ..., "data_type": ...}, ...]) so the AST
    layer can validate columns, not just tables. Update the caller to pass
    this through (it's already available from grade_sample.py's output).

    Returns:
        {"sql": str, "params": list, "table": str}

    Raises SafetyViolationError if either the build layer or the AST
    verification layer rejects the query.
    """
    try:
        joins = query.get("joins", [])
        tables_used = [query.get("table")]
        if isinstance(joins, list):
            tables_used.extend(
                join.get("table")
                for join in joins
                if isinstance(join, dict)
            )
        blocked_tables = find_blocked_tables(
            table for table in tables_used if isinstance(table, str)
        )
        if blocked_tables:
            raise SafetyViolationError(
                f"Table(s) not allowed: {', '.join(blocked_tables)}",
                rule_id="blocked_table",
            )

        # Layer 1: build
        sql, params, safe_table, row_limit, count_sql, count_params = _build_sql(
            query, valid_tables, schema_context, relationships or []
        )

        # Layer 2: independently re-verify the built SQL via AST
        _ast_verify(
            sql,
            schema_context,
            relationships,
            query.get("count_entity"),
            expected_query=query,
        )
    except SafetyViolationError as exc:
        logger.error(
            "Safety check rejected query plan: %s",
            _rejected_plan_log_json(query, str(exc)),
            extra={"event": "safety_check_rejected_plan"},
        )
        raise

    logger.info(
        "Safety check (build + AST) passed for %d table(s)",
        1 + len(query.get("joins", [])),
    )

    return {
        "sql": sql,
        "params": params,
        "table": safe_table,
        "row_limit": row_limit,
        "user_limit": query.get("limit") is not None,
        "count_sql": count_sql,
        "count_params": count_params,
    }


def _rejected_plan_log_json(query: Dict[str, Any], reason: str) -> str:
    """Serialize a bounded plan summary without logging user-supplied values."""
    safe_plan: Dict[str, Any] = {}
    for key in ("table", "joins", "select_items", "where_expression",
                "group_by", "having_expression", "order_by", "limit"):
        if key not in query:
            continue
        value = query[key]
        if key in {"where_expression", "having_expression"}:
            safe_plan[key] = _redact_filter_values(value)
        elif key == "select_items" and isinstance(value, list):
            safe_plan[key] = [
                {
                    field: _bounded_log_atom(item[field])
                    for field in ("table", "column", "aggregate", "distinct", "alias")
                    if field in item
                }
                for item in value[:16]
                if isinstance(item, dict)
            ]
        elif key == "joins" and isinstance(value, list):
            safe_plan[key] = [
                {
                    **{
                        field: str(join[field])[:128]
                        for field in ("table", "join_type")
                        if field in join
                    },
                    **(
                        {
                            "relationship": {
                                field: str(join["relationship"][field])[:128]
                                for field in (
                                    "constraint_name", "table", "referenced_table"
                                )
                                if field in join["relationship"]
                                and isinstance(join["relationship"], dict)
                            }
                        }
                        if isinstance(join.get("relationship"), dict)
                        else {}
                    ),
                    **(
                        {
                            "conditions": [
                                {
                                    field: str(cond[field])[:128]
                                    for field in ("table", "column", "operator", "value")
                                    if field in cond
                                }
                                for cond in join.get("conditions", [])[:8]
                                if isinstance(cond, dict)
                            ]
                        }
                        if isinstance(join.get("conditions"), list)
                        else {}
                    ),
                }
                for join in value[:8]
                if isinstance(join, dict)
            ]
        elif key in {"table", "limit"}:
            safe_plan[key] = str(value)[:128]
        elif key == "group_by" and isinstance(value, list):
            safe_plan[key] = [
                {
                    field: str(item[field])[:128]
                    for field in ("table", "column")
                    if field in item
                }
                for item in value[:16]
                if isinstance(item, dict)
            ]
        elif key == "order_by" and isinstance(value, list):
            safe_plan[key] = [
                {
                    field: str(item[field])[:128]
                    for field in ("table", "column", "direction")
                    if field in item
                }
                for item in value[:16]
                if isinstance(item, dict)
            ]
        else:
            safe_plan[key] = _bounded_log_atom(value)

    record = {
        "reason": reason[:512],
        "plan": safe_plan,
    }
    serialized = json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str)
    if len(serialized) <= _MAX_REJECTED_PLAN_LOG_CHARS:
        return serialized

    compact_plan = {
        key: safe_plan[key]
        for key in ("table", "joins", "select_items", "group_by", "order_by", "limit")
        if key in safe_plan
    }
    compact_record = {
        "reason": reason[:512],
        "plan": compact_plan,
        "truncated": True,
    }
    serialized = json.dumps(
        compact_record, ensure_ascii=True, separators=(",", ":"), default=str
    )
    if len(serialized) <= _MAX_REJECTED_PLAN_LOG_CHARS:
        return serialized

    return json.dumps({
        "reason": reason[:256],
        "plan": {"table": str(query.get("table", ""))[:128]},
        "truncated": True,
    }, ensure_ascii=True, separators=(",", ":"))


def _redact_filter_values(expression: Any, depth: int = 0) -> Any:
    if depth >= 8:
        return "<truncated>"
    if isinstance(expression, dict):
        return {
            key: ("<redacted>" if key == "value" else (
                _redact_filter_values(value, depth + 1)
                if key == "conditions"
                else str(value)[:128]
            ))
            for key, value in expression.items()
            if key in {
                "boolean", "conditions", "table", "column", "operator",
                "aggregate", "value",
            }
        }
    if isinstance(expression, list):
        return [
            _redact_filter_values(item, depth + 1)
            for item in expression[:64]
        ]
    return str(expression)[:128]


def _bounded_log_atom(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:128]
    return f"<{type(value).__name__}>"