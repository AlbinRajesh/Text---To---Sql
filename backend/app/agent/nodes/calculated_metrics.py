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
    if not isinstance(condition, dict) or set(condition) != {
        "table", "column", "operator", "value"
    }:
        raise ValueError(
            "share_of_rows requires a condition (numeric table, column, comparison operator and value) placed inside the select item itself, not in where_expression."
        )
    table = condition.get("table")
    column = condition.get("column")
    operator = condition.get("operator")
    if not isinstance(table, str) or table not in set(query_tables):
        raise ValueError("share_of_rows condition must use a table in the query.")
    if not isinstance(column, str):
        raise ValueError("share_of_rows condition must identify a schema column.")
    if operator not in {"=", "!=", "<>", ">", ">=", "<", "<="}:
        raise ValueError("share_of_rows supports numeric comparison operators only.")
    available = _column_types(schema_context, table)
    data_type = available.get(column)
    if data_type not in _NUMERIC_TYPES:
        raise ValueError(
            f"share_of_rows condition requires a verified numeric column "
            f"{table}.{column}."
        )
    value = condition.get("value")
    if isinstance(value, (bool, dict, list)) or value is None:
        raise ValueError("share_of_rows condition value must be a finite number.")
    try:
        numeric_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("share_of_rows condition value must be a finite number.") from exc
    if not numeric_value.is_finite():
        raise ValueError("share_of_rows condition value must be a finite number.")
    decimal_tuple = numeric_value.as_tuple()
    if len(decimal_tuple.digits) > 64 or abs(decimal_tuple.exponent) > 64:
        raise ValueError("share_of_rows condition value is outside the supported range.")
    if len(format(numeric_value, "f")) > 64:
        raise ValueError("share_of_rows condition value is outside the supported range.")
    return {
        "table": table,
        "column": column,
        "operator": operator,
        "value": numeric_value,
    }


def compile_share_of_rows(
    condition: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> str:
    checked = validate_share_condition(condition, schema_context, query_tables)
    column = _quote_column(checked["table"], checked["column"])
    value = format(checked["value"], "f")
    return (
        f"(SUM(CASE WHEN {column} {checked['operator']} {value} "
        f"THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0))"
    )


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
