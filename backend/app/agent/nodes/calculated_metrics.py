"""Code-owned numeric metrics compiled only from verified schema columns."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable


MAX_METRIC_DEPTH = 8
_NUMERIC_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year",
    "decimal", "numeric", "float", "double", "real",
}


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    table: str
    columns: tuple[str, ...]
    dependencies: tuple[str, ...]
    build: Callable[[Callable[[str], str], Callable[[str], str]], str]
    unit: str


def _quote_column(table: str, column: str) -> str:
    return f"`{table.replace('`', '``')}`.`{column.replace('`', '``')}`"


def _build_final_price(column: Callable[[str], str], metric: Callable[[str], str]) -> str:
    return column("offer_price")


def _build_savings(column: Callable[[str], str], metric: Callable[[str], str]) -> str:
    return f"({column('actual_price')} - {metric('final_price')})"


def _build_discount_percent(
    column: Callable[[str], str], metric: Callable[[str], str]
) -> str:
    price_discount = (
        f"({metric('savings')} * 100.0 / "
        f"NULLIF({column('actual_price')}, 0))"
    )
    return (
        "CASE "
        f"WHEN {column('actual_price')} > 0 "
        f"AND {column('offer_price')} > 0 "
        f"AND {column('offer_price')} < {column('actual_price')} "
        f"AND (COALESCE({column('offer_price_p')}, 0) = 0 "
        f"OR ABS({price_discount} - {column('offer_price_p')}) <= 1) "
        f"THEN {price_discount} "
        f"WHEN {column('offer_price')} IS NULL "
        f"AND {column('offer_price_p')} BETWEEN 1 AND 100 "
        f"THEN {column('offer_price_p')} "
        "ELSE NULL END"
    )


def _build_coupon_discount_percent(
    column: Callable[[str], str], metric: Callable[[str], str]
) -> str:
    return (
        f"CASE WHEN {column('coupon_price_p')} BETWEEN 1 AND 100 "
        f"THEN {column('coupon_price_p')} ELSE NULL END"
    )


def validate_share_condition(
    condition: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> Dict[str, Any]:
    if not isinstance(condition, dict):
        raise ValueError(
            "share_of_rows requires a verified numeric condition inside the "
            "select item, not in where_expression."
        )
    operator = condition.get("operator")
    allowed_operators = {
        "=", "!=", "<>", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE",
        "IS NULL", "IS NOT NULL",
    }
    if not isinstance(operator, str) or operator not in allowed_operators:
        raise ValueError("share_of_rows uses an unsupported condition operator.")
    nullable_check = operator in {"IS NULL", "IS NOT NULL"}
    metric_name = condition.get("metric")
    if metric_name is not None:
        expected_keys = {"metric", "operator"}
        if not nullable_check:
            expected_keys.add("value")
        actual_keys = set(condition)
        allowed_key_sets = {
            frozenset(expected_keys),
            frozenset(expected_keys | {"table"}),
        }
        if (
            frozenset(actual_keys) not in allowed_key_sets
            and not (
                nullable_check
                and condition.get("value") is None
                and frozenset(actual_keys - {"value"}) in allowed_key_sets
            )
        ):
            raise ValueError(
                "A metric share condition must contain only metric, operator, "
                "and a value when the operator requires one."
            )
        definition = validate_metric(metric_name, schema_context, query_tables)
        table = definition.table
        if condition.get("table", table) != table:
            raise ValueError("Metric share condition table does not match its metric.")
        if operator == "LIKE":
            raise ValueError("share_of_rows LIKE requires a categorical column.")
        column = None
        data_type = "numeric"
    else:
        expected_keys = {"table", "column", "operator"}
        if not nullable_check:
            expected_keys.add("value")
        actual_keys = set(condition)
        if (
            actual_keys != expected_keys
            and not (
                nullable_check
                and condition.get("value") is None
                and actual_keys - {"value"} == expected_keys
            )
        ):
            raise ValueError(
                "share_of_rows requires one verified table/column condition "
                "with a value when its operator requires one, placed inside "
                "the select item itself, not in where_expression."
            )
        table = condition.get("table")
        column = condition.get("column")
        if not isinstance(table, str) or table not in set(query_tables):
            raise ValueError("share_of_rows condition must use a table in the query.")
        if not isinstance(column, str):
            raise ValueError("share_of_rows condition must identify a schema column.")
        available = _column_types(schema_context, table)
        data_type = available.get(column)
        if data_type is None:
            raise ValueError(
                f"share_of_rows condition requires a verified column {table}.{column}."
            )
        numeric_column = data_type in _NUMERIC_TYPES
        categorical_operators = {
            "=", "!=", "<>", "IN", "NOT IN", "LIKE", "IS NULL", "IS NOT NULL",
        }
        if not numeric_column and operator not in categorical_operators:
            raise ValueError(
                f"share_of_rows operator {operator!r} requires a numeric column; "
                f"{table}.{column} is {data_type!r}."
            )
        if numeric_column and operator == "LIKE":
            raise ValueError("share_of_rows LIKE requires a categorical column.")

    normalized_value = None
    if not nullable_check:
        value = condition.get("value")
        if operator in {"IN", "NOT IN"}:
            if not isinstance(value, list) or not value or len(value) > 100:
                raise ValueError(
                    f"share_of_rows {operator} requires a non-empty value list "
                    "of at most 100 items."
                )
            values = value
        else:
            values = [value]

        if any(isinstance(item, (bool, dict, list)) or item is None for item in values):
            raise ValueError("share_of_rows condition value must be a valid scalar.")

        if metric_name is not None or data_type in _NUMERIC_TYPES:
            normalized_values = []
            for item in values:
                try:
                    numeric_value = Decimal(str(item))
                except (InvalidOperation, ValueError) as exc:
                    raise ValueError(
                        "share_of_rows condition value must be a finite number."
                    ) from exc
                if not numeric_value.is_finite():
                    raise ValueError(
                        "share_of_rows condition value must be a finite number."
                    )
                decimal_tuple = numeric_value.as_tuple()
                if (
                    len(decimal_tuple.digits) > 64
                    or abs(decimal_tuple.exponent) > 64
                    or len(format(numeric_value, "f")) > 64
                ):
                    raise ValueError(
                        "share_of_rows condition value is outside the supported range."
                    )
                normalized_values.append(numeric_value)
            normalized_value = (
                normalized_values
                if operator in {"IN", "NOT IN"}
                else normalized_values[0]
            )
        else:
            if any(not isinstance(item, str) for item in values):
                raise ValueError(
                    "Categorical share condition values must be strings."
                )
            normalized_value = values if operator in {"IN", "NOT IN"} else values[0]

    normalized = {
        "table": table,
        "operator": operator,
    }
    if not nullable_check:
        normalized["value"] = normalized_value
    if metric_name is not None:
        normalized["metric"] = metric_name
    else:
        normalized["column"] = column
    return normalized


def compile_share_of_rows(
    condition: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
    *,
    grouped_distribution: bool = False,
) -> str:
    checked = validate_share_condition(condition, schema_context, query_tables)
    column = (
        compile_metric(checked["metric"], schema_context, query_tables)
        if checked.get("metric") is not None
        else _quote_column(checked["table"], checked["column"])
    )
    operator = checked["operator"]
    if operator in {"IS NULL", "IS NOT NULL"}:
        predicate = f"{column} {operator}"
    elif operator in {"IN", "NOT IN"}:
        values = ", ".join(
            _format_share_value(value) for value in checked["value"]
        )
        predicate = f"{column} {operator} ({values})"
    else:
        predicate = (
            f"{column} {operator} "
            f"{_format_share_value(checked['value'])}"
        )
    denominator = (
        "SUM(COUNT(*)) OVER ()" if grouped_distribution else "COUNT(*)"
    )
    return (
        f"(SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) * 100.0 "
        f"/ NULLIF({denominator}, 0))"
    )


def _format_share_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


METRIC_REGISTRY: Dict[str, MetricDefinition] = {
    "final_price": MetricDefinition(
        name="final_price",
        table="offer",
        columns=("offer_price",),
        dependencies=(),
        build=_build_final_price,
        unit="currency",
    ),
    "savings": MetricDefinition(
        name="savings",
        table="offer",
        columns=("actual_price",),
        dependencies=("final_price",),
        build=_build_savings,
        unit="currency",
    ),
    "discount_percent": MetricDefinition(
        name="discount_percent",
        table="offer",
        columns=("actual_price", "offer_price", "offer_price_p"),
        dependencies=("savings",),
        build=_build_discount_percent,
        unit="percentage",
    ),
    "coupon_discount_percent": MetricDefinition(
        name="coupon_discount_percent",
        table="offer",
        columns=("coupon_price_p",),
        dependencies=(),
        build=_build_coupon_discount_percent,
        unit="percentage",
    ),
}


def _column_types(schema_context: Dict[str, Any], table: str) -> Dict[str, str]:
    return {
        item["column"]: item["data_type"].casefold()
        for item in schema_context.get(table, [])
        if isinstance(item, dict)
        and isinstance(item.get("column"), str)
        and isinstance(item.get("data_type"), str)
    }


def validate_metric(
    name: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> MetricDefinition:
    """Fail closed unless every required numeric source column is in the live schema."""
    if not isinstance(name, str) or name not in METRIC_REGISTRY:
        raise ValueError(f"Unsupported calculated metric {name!r}.")
    definition = METRIC_REGISTRY[name]
    if definition.table not in set(query_tables):
        raise ValueError(
            f"Calculated metric {name!r} requires table {definition.table!r}."
        )

    available = _column_types(schema_context, definition.table)
    for column in definition.columns:
        data_type = available.get(column)
        if data_type is None:
            raise ValueError(
                f"Calculated metric {name!r} requires verified column "
                f"{definition.table}.{column}."
            )
        if data_type not in _NUMERIC_TYPES:
            raise ValueError(
                f"Calculated metric source {definition.table}.{column} "
                f"must be numeric, got {data_type!r}."
            )

    for dependency in definition.dependencies:
        validate_metric(dependency, schema_context, query_tables)
    return definition


def compile_metric(
    name: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
    *,
    _depth: int = 0,
) -> str:
    """Compile a registry key to a SQL fragment; callers never supply SQL text."""
    if _depth >= MAX_METRIC_DEPTH:
        raise ValueError(
            f"Calculated metric nesting exceeds {MAX_METRIC_DEPTH} levels."
        )
    definition = validate_metric(name, schema_context, query_tables)
    columns = lambda column: _quote_column(definition.table, column)
    metrics = lambda dependency: compile_metric(
        dependency,
        schema_context,
        query_tables,
        _depth=_depth + 1,
    )
    return definition.build(columns, metrics)
