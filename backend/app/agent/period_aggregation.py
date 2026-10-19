"""Validation for the typed half-open date ranges used by conditional metrics."""

from datetime import date
from typing import Any, Dict, Iterable


_DATE_TYPES = {"date", "datetime", "timestamp"}
_NUMERIC_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
    "decimal", "numeric", "float", "double", "real",
}


def normalize_period(
    period: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> Dict[str, str]:
    """Validate and normalize a period predicate against known query schema."""
    if not isinstance(period, dict) or set(period) != {
        "table", "column", "start", "end"
    }:
        raise ValueError(
            "A period condition must contain exactly table, column, start, and end."
        )
    table, column = period.get("table"), period.get("column")
    if not isinstance(table, str) or table not in set(query_tables):
        raise ValueError("A period condition must reference a table in the query.")
    if not isinstance(column, str) or not column:
        raise ValueError("A period condition must identify a date column.")
    metadata = schema_context.get(table, [])
    column_info = next(
        (
            entry for entry in metadata
            if isinstance(entry, dict) and entry.get("column") == column
        ),
        None,
    )
    if column_info is None:
        raise ValueError(f"Period column {table}.{column} is not in the schema.")
    data_type = str(column_info.get("data_type", "")).lower().split("(", 1)[0]
    if data_type not in _DATE_TYPES:
        raise ValueError(
            f"Period column {table}.{column} must be a date, datetime, or timestamp."
        )
    start, end = period.get("start"), period.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("Period boundaries must be ISO date strings.")
    try:
        parsed_start = date.fromisoformat(start)
        parsed_end = date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("Period boundaries must be valid ISO dates (YYYY-MM-DD).") from exc
    if start != parsed_start.isoformat() or end != parsed_end.isoformat():
        raise ValueError("Period boundaries must use canonical YYYY-MM-DD format.")
    if parsed_start >= parsed_end:
        raise ValueError("Period start must be earlier than period end.")
    return {
        "table": table,
        "column": column,
        "start": start,
        "end": end,
    }


def normalize_period_comparison(
    comparison: Any,
    schema_context: Dict[str, Any],
    query_tables: Iterable[str],
) -> Dict[str, Dict[str, str]]:
    """Validate a later-minus-earlier SUM comparison over two disjoint periods."""
    if not isinstance(comparison, dict) or set(comparison) != {
        "later_period", "earlier_period"
    }:
        raise ValueError(
            "A period comparison must contain exactly later_period and earlier_period."
        )
    later = normalize_period(
        comparison["later_period"], schema_context, query_tables
    )
    earlier = normalize_period(
        comparison["earlier_period"], schema_context, query_tables
    )
    if (
        later["table"] != earlier["table"]
        or later["column"] != earlier["column"]
        or earlier["end"] > later["start"]
    ):
        raise ValueError(
            "Compared periods must use the same date column and must not overlap."
        )
    if later["start"] <= earlier["start"]:
        raise ValueError("later_period must begin after earlier_period.")
    return {"later_period": later, "earlier_period": earlier}


def validate_period_sum_column(
    table: str,
    column: str,
    schema_context: Dict[str, Any],
) -> None:
    """Require SUM period metrics to reference a known numeric schema column."""
    column_info = next(
        (
            entry
            for entry in schema_context.get(table, [])
            if isinstance(entry, dict) and entry.get("column") == column
        ),
        None,
    )
    if column_info is None:
        raise ValueError(f"SUM column {table}.{column} is not in the schema.")
    data_type = str(column_info.get("data_type", "")).lower().split("(", 1)[0]
    if data_type not in _NUMERIC_TYPES:
        raise ValueError(f"SUM column {table}.{column} must be numeric.")
