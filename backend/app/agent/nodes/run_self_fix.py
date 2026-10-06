"""
run_self_fix.py  (Node 8: Run + Self-Fix — MySQL version)

BASIC VERSION — Phase 1 scope. Runs the validated, safety-checked SQL query
against the READ REPLICA and returns results or a clear error. The
"self-fix" retry loop (send MySQL error back to the AI, retry up to 3x)
is Phase 2 and not implemented yet — a failure here propagates as an
error, which the caller should route to Node 11 (escalation).
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

import aiomysql

from app.db.mysql_client import fetch_all_readonly

logger = logging.getLogger(__name__)


class QueryExecutionError(Exception):
    """
    Raised when the validated SQL query fails to execute against MySQL.
    In Phase 1, this is terminal — caller routes to Node 11 (escalation).
    In Phase 2, this becomes the trigger for the self-fix retry loop.
    """
    pass


def _serialize_value(value: Any) -> Any:
    """
    Converts MySQL result values (datetime, date, Decimal, bytes) into
    JSON-safe types before returning results to the API layer / frontend.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    return value


async def run_query(validated_query: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry point for Node 8 (basic version).

    Takes the output of Node 7 (safety_check) — an already-built, parameterized
    SQL string plus its param values — and executes it against the read
    replica only.

    Expects:
        {
            "sql": str,               # fully assembled, parameterized SQL
            "params": tuple,          # values for the %s placeholders
            "row_limit": int,         # maximum rows to return after probing one extra
            "user_limit": bool,       # whether the user requested a result limit
            "count_sql": str | None,  # safe count query for scalar aggregate diagnostics
            "count_params": tuple,    # bound values for count_sql
        }

    Returns:
        {
            "results": list[dict],    # JSON-safe, ready to send to the frontend
            "columns": list[str],
            "count": int,
            "row_count": int,
            "truncated": bool,
            "row_limit": int,
            "no_data_reason": "no_rows" | "all_null" | None,
        }

    Raises QueryExecutionError on any MySQL-side failure (bad column
    reference, timeout, connection issue) — deliberately not caught and
    retried here yet, since that retry logic is Phase 2 (self-fix).
    """
    sql = validated_query["sql"]
    params = validated_query.get("params", ())
    row_limit = validated_query.get("row_limit")
    if not isinstance(row_limit, int) or row_limit < 1:
        raise QueryExecutionError("A positive row_limit is required for query execution.")
    user_limit = validated_query.get("user_limit", False)
    if not isinstance(user_limit, bool):
        raise QueryExecutionError("user_limit must be a boolean.")

    try:
        raw_rows = await fetch_all_readonly(sql, params)
    except aiomysql.MySQLError as e:
        # MySQL's MAX_EXECUTION_TIME session limit (set in mysql_client.py)
        # raises a generic MySQLError with a timeout-related error code,
        # not a distinct timeout exception like PyMongo's — so we check
        # the error code instead of catching a separate exception type.
        if e.args and e.args[0] == 3024:  # ER_QUERY_TIMEOUT
            raise QueryExecutionError(
                "Query exceeded the session time limit. It may be too broad "
                "for this filter, or the table too large."
            ) from e
        raise QueryExecutionError(f"Query failed: {e}") from e
    except Exception as e:
        raise QueryExecutionError(f"Query failed: {e}") from e

    truncated = not user_limit and len(raw_rows) > row_limit

    # The probe row (row_limit + 1) is trimmed below. Before that, check whether
    # it ties with the last kept row on the sort column: if so, "top N" has no
    # unique answer.
    tie_at_limit = False
    tie_column = validated_query.get("tie_column")
    declared_columns = validated_query.get("columns", [])
    if (
        user_limit
        and isinstance(tie_column, str)
        and tie_column in declared_columns
        and len(raw_rows) > row_limit
    ):
        tie_index = declared_columns.index(tie_column)
        last_kept = raw_rows[row_limit - 1][tie_index]
        probe = raw_rows[row_limit][tie_index]
        tie_at_limit = last_kept is not None and last_kept == probe

    raw_rows = raw_rows[:row_limit]

    no_data_reason = None
    matching_row_count = None
    if raw_rows and all(value is None for value in raw_rows[0]) and len(raw_rows) == 1:
        count_sql = validated_query.get("count_sql")
        count_params = validated_query.get("count_params", ())
        if count_sql is not None:
            if not isinstance(count_sql, str) or not isinstance(count_params, (tuple, list)):
                raise QueryExecutionError("Scalar aggregate diagnostic query is invalid.")
            try:
                count_rows = await fetch_all_readonly(count_sql, tuple(count_params))
            except aiomysql.MySQLError as e:
                raise QueryExecutionError(
                    f"Aggregate empty-result diagnostic failed: {e}"
                ) from e
            except Exception as e:
                raise QueryExecutionError(
                    f"Aggregate empty-result diagnostic failed: {e}"
                ) from e
            if (
                len(count_rows) != 1
                or len(count_rows[0]) != 1
                or type(count_rows[0][0]) is not int
                or count_rows[0][0] < 0
            ):
                raise QueryExecutionError(
                    "Aggregate empty-result diagnostic returned an invalid count."
                )
            matching_row_count = count_rows[0][0]
            no_data_reason = "no_rows" if matching_row_count == 0 else "all_null"
            raw_rows = []

    # fetch_all_readonly returns raw tuples with no column names attached —
    # column names must come from the safety-checked query's SELECT clause,
    # since cursor.description isn't exposed through fetch_all_readonly.
    columns = validated_query.get("columns", [])

    column_formats = validated_query.get("column_formats", {})
    serialized_results: List[Dict[str, Any]] = []
    for row in raw_rows:
        serialized_row = {}
        for col, value in zip(columns, row):
            serialized_value = _serialize_value(value)
            if (
                column_formats.get(col) == "identifier"
                and serialized_value is not None
            ):
                serialized_value = str(serialized_value)
            serialized_row[col] = serialized_value
        serialized_results.append(serialized_row)

    logger.info(
        "Executed query: %d row(s) returned",
        len(serialized_results),
    )

    return {
        "results": serialized_results,
        "columns": columns,
        "count": len(serialized_results),
        "row_count": len(serialized_results),
        "truncated": truncated,
        "row_limit": row_limit,
        "no_data_reason": no_data_reason,
        "matching_row_count": matching_row_count,
        "tie_at_limit": tie_at_limit,
        "column_formats": column_formats,
        "suggestions": [],
    }