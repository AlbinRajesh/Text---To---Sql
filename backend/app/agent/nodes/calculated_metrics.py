"""Code-owned numeric metrics compiled only from verified schema columns."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable


MAX_METRIC_DEPTH = 8
GROUP_DISTRIBUTION_PERCENTAGE = "group_distribution_percentage"
_NUMERIC_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year",
    "decimal", "numeric", "float", "double", "real",
}
_NON_GROUPABLE_TYPES = {
    "blob", "tinyblob", "mediumblob", "longblob", "json", "geometry",
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
    if operator not in {"=", "!=", "<>", ">", ">=", "<", "<="}:
        raise ValueError("share_of_rows supports numeric comparison operators only.")
    metric_name = condition.get("metric")
    if metric_name is not None:
        if set(condition) not in (
            {"metric", "operator", "value"},
            {"metric", "table", "operator", "value"},
        ):
            raise ValueError(
                "A metric share condition must contain only metric, operator, and value."
            )
        definition = validate_metric(metric_name, schema_context, query_tables)
        table = definition.table
        if condition.get("table", table) != table:
            raise ValueError("Metric share condition table does not match its metric.")
        column = None
    else:
        if set(condition) != {"table", "column", "operator", "value"}:
            raise ValueError(
                "share_of_rows requires a condition (numeric table, column, "
                "comparison operator and value) placed inside the select item "
                "itself, not in where_expression."
            )
        table = condition.get("table")
        column = condition.get("column")
        if not isinstance(table, str) or table not in set(query_tables):
            raise ValueError("share_of_rows condition must use a table in the query.")
        if not isinstance(column, str):
            raise ValueError("share_of_rows condition must identify a schema column.")
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
    normalized = {
        "table": table,
        "operator": operator,
        "value": numeric_value,
    }
    if metric_name is not None:
        normalized["metric"] = metric_name
    else:
        normalized["column"] = column
    return normalized


def compile_share_of_rows(
    condition: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> str:
    checked = validate_share_condition(condition, schema_context, query_tables)
    column = (
        compile_metric(checked["metric"], schema_context, query_tables)
        if checked.get("metric") is not None
        else _quote_column(checked["table"], checked["column"])
    )


def validate_group_distribution_dimension(
    table: Any,
    column: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> tuple[str, str]:
    if (
        not isinstance(table, str)
        or table not in set(query_tables)
        or not isinstance(column, str)
    ):
        raise ValueError(
            "A grouped percentage distribution must identify a query column."
        )
    column_info = next(
        (
            item
            for item in schema_context.get(table, [])
            if isinstance(item, dict) and item.get("column") == column
        ),
        None,
    )
    data_type = (
        column_info.get("data_type", "").casefold()
        if isinstance(column_info, dict)
        and isinstance(column_info.get("data_type"), str)
        else ""
    )
    if column_info is None or not data_type or data_type in _NON_GROUPABLE_TYPES:
        raise ValueError(
            "A grouped percentage distribution requires a verified groupable column."
        )
    return table, column


def validate_group_distribution_plan(
    query: Dict[str, Any],
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> bool:
    """Validate a grouped percentage distribution over one verified dimension."""
    query_table_set = set(query_tables)
    items = query.get("select_items", [])
    metric_items = [
        item for item in items
        if isinstance(item, dict)
        and item.get("metric") == GROUP_DISTRIBUTION_PERCENTAGE
    ]
    if not metric_items:
        return False
    if len(metric_items) != 1 or len(query_table_set) != 1:
        raise ValueError(
            "A grouped percentage distribution requires one base table and one metric."
        )
    metric_item = metric_items[0]
    table, column = metric_item.get("table"), metric_item.get("column")
    if (
        table != query.get("table")
        or metric_item.get("aggregate") is not None
        or metric_item.get("distinct", False)
        or query.get("having_expression") is not None
        or query.get("count_groups", False)
    ):
        raise ValueError(
            "A grouped percentage distribution must measure one base-table dimension."
        )
    validate_group_distribution_dimension(
        table, column, schema_context, query_table_set
    )

    group_by = query.get("group_by")
    if (
        not isinstance(group_by, list)
        or len(group_by) != 1
        or not isinstance(group_by[0], dict)
        or group_by[0].get("table", table) != table
        or group_by[0].get("column") != column
        or group_by[0].get("date_bucket") is not None
    ):
        raise ValueError(
            "A grouped percentage distribution must group by its exact dimension."
        )
    if not any(
        isinstance(item, dict)
        and item.get("metric") is None
        and item.get("table", table) == table
        and item.get("column") == column
        and item.get("aggregate") is None
        for item in items
    ):
        raise ValueError(
            "A grouped percentage distribution must also select its dimension."
        )

    def has_non_null_filter(expression: Any) -> bool:
        if not isinstance(expression, dict):
            return False
        boolean = expression.get("boolean")
        if boolean == "AND":
            return any(
                has_non_null_filter(condition)
                for condition in expression.get("conditions", [])
            )
        if boolean == "OR":
            conditions = expression.get("conditions", [])
            return bool(conditions) and all(
                has_non_null_filter(condition) for condition in conditions
            )
        if boolean is not None:
            return False
        return (
            expression.get("table", table) == table
            and expression.get("column") == column
            and expression.get("operator") == "IS NOT NULL"
        )

    if not has_non_null_filter(query.get("where_expression")):
        raise ValueError(
            "A grouped percentage distribution must exclude NULL dimension values."
        )
    return True


def compile_group_distribution_percentage() -> str:
    """Compile the share of filtered, non-NULL base rows in each group."""
    return (
        "COUNT(*) * 100.0 / "
        "NULLIF(SUM(COUNT(*)) OVER (), 0)"
    )
    value = format(checked["value"], "f")
    return (
        f"(SUM(CASE WHEN {column} {checked['operator']} {value} "
        f"THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0))"
    )


def _build_booking_discount_percent(
    column: Callable[[str], str], metric: Callable[[str], str]
) -> str:
    return (
        f"({column('discount')} * 100.0 / "
        f"NULLIF({column('actual_amount')}, 0))"
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
    "booking_discount_percent": MetricDefinition(
        name="booking_discount_percent",
        table="bookings",
        columns=("discount", "actual_amount"),
        dependencies=(),
        build=_build_booking_discount_percent,
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
