"""
run_self_fix.py (Node 8: Run + Self-Fix — MySQL version)

Runs safety-checked SQL against the read replica. Transient infrastructure
retries are separate from bounded model-driven query repair.
"""

import asyncio
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List

import aiomysql

from app.db.mysql_client import fetch_all_readonly
from app.services.errors import InfrastructureError

logger = logging.getLogger(__name__)

MAX_EXECUTION_RETRIES = max(1, int(os.getenv("DB_QUERY_MAX_RETRIES", "3")))
EXECUTION_BASE_BACKOFF_SECONDS = float(
    os.getenv("DB_QUERY_BASE_BACKOFF_SECONDS", "0.25")
)
TRANSIENT_MYSQL_CODES = {2002, 2003, 2006, 2013, 2055}
REPAIRABLE_MYSQL_CODES = {1052, 1054, 1055, 1064}
MAX_QUERY_REPAIR_ATTEMPTS = 3


class QueryExecutionError(Exception):
    """
    Raised when the validated SQL query fails to execute against MySQL.
    Non-transient query failures are terminal and route to Node 11.
    """
    def __init__(self, message: str, *, mysql_code: int | None = None) -> None:
        Exception.__init__(self, message)
        self.mysql_code = mysql_code


class QueryRepairError(Exception):
    """A candidate plan failed validation before it could safely be executed."""

    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


class QueryInfrastructureError(QueryExecutionError, InfrastructureError):
    """Raised when a transient database failure persists after retries."""

    def __init__(self, message: str, code: str) -> None:
        QueryExecutionError.__init__(self, message)
        self.code = code
        self.infrastructure_code = code
        self.stage = "execute"


async def _fetch_with_retries(
    sql: str,
    params: tuple[Any, ...],
    operation: str,
) -> list[tuple[Any, ...]]:
    last_error: BaseException | None = None
    failure_code = "DB_CONNECTION_ERROR"
    for attempt in range(1, MAX_EXECUTION_RETRIES + 1):
        try:
            return await fetch_all_readonly(sql, params)
        except aiomysql.MySQLError as exc:
            error_code = exc.args[0] if exc.args else None
            if error_code == 3024:
                failure_code = "DB_QUERY_TIMEOUT"
                last_error = exc
            elif error_code in TRANSIENT_MYSQL_CODES:
                failure_code = "DB_CONNECTION_ERROR"
                last_error = exc
            else:
                mysql_code = (
                    error_code
                    if isinstance(error_code, int) and not isinstance(error_code, bool)
                    else None
                )
                raise QueryExecutionError(
                    f"{operation} failed: {exc}",
                    mysql_code=mysql_code,
                ) from exc
        except (asyncio.TimeoutError, TimeoutError, OSError) as exc:
            failure_code = "DB_TIMEOUT" if isinstance(
                exc, (asyncio.TimeoutError, TimeoutError)
            ) else "DB_CONNECTION_ERROR"
            last_error = exc

        if attempt == MAX_EXECUTION_RETRIES:
            break
        delay = EXECUTION_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
        logger.warning(
            "%s transient failure (%s), retry %d/%d in %.2fs",
            operation,
            failure_code,
            attempt + 1,
            MAX_EXECUTION_RETRIES,
            delay,
        )
        await asyncio.sleep(delay)

    message = (
        f"{operation} failed after {MAX_EXECUTION_RETRIES} attempts "
        f"({failure_code}): {last_error}"
    )
    if failure_code == "DB_QUERY_TIMEOUT":
        message += ". The query may be too broad or the database may be overloaded."
    raise QueryInfrastructureError(message, failure_code) from last_error


async def run_query_with_repair(
    validated_query: Dict[str, Any],
    repair: Callable[
        [QueryExecutionError, int],
        Awaitable[Dict[str, Any]],
    ],
    *,
    max_attempts: int = MAX_QUERY_REPAIR_ATTEMPTS,
    executor: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]] | None = None,
) -> Dict[str, Any]:
    """Execute and repair only recognized SQL-shape errors, at most three times."""
    if (
        type(max_attempts) is not int
        or not 1 <= max_attempts <= MAX_QUERY_REPAIR_ATTEMPTS
    ):
        raise ValueError(
            f"max_attempts must be between 1 and {MAX_QUERY_REPAIR_ATTEMPTS}."
        )

    execute = executor or run_query
    try:
        return await execute(validated_query)
    except QueryExecutionError as exc:
        if (
            isinstance(exc, InfrastructureError)
            or exc.mysql_code not in REPAIRABLE_MYSQL_CODES
            or max_attempts == 1
        ):
            raise
        last_error = exc

    current_query = validated_query
    for repair_attempt in range(1, max_attempts):
        logger.warning(
            "Query execution validation failed with MySQL code %s; "
            "requesting repair %d/%d: %s",
            last_error.mysql_code,
            repair_attempt,
            max_attempts - 1,
            last_error,
        )
        try:
            current_query = await repair(last_error, repair_attempt)
        except QueryRepairError as exc:
            if exc.terminal:
                raise
            logger.warning(
                "Query repair %d/%d returned an invalid plan: %s",
                repair_attempt,
                max_attempts - 1,
                exc,
            )
            last_error = QueryExecutionError(
                f"Query repair failed validation: {exc}",
                mysql_code=last_error.mysql_code,
            )
            continue

        try:
            return await execute(current_query)
        except QueryExecutionError as exc:
            if (
                isinstance(exc, InfrastructureError)
                or exc.mysql_code not in REPAIRABLE_MYSQL_CODES
            ):
                raise
            last_error = exc

    raise QueryExecutionError(
        f"Query execution and repair exhausted {max_attempts} total attempts: "
        f"{last_error}",
        mysql_code=last_error.mysql_code,
    ) from last_error


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
    Execute one safety-checked SQL plan against the read replica.

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

    Raises QueryExecutionError on non-transient MySQL-side failures and
    QueryInfrastructureError after bounded retries for transient failures.
    """
    sql = validated_query["sql"]
    params = validated_query.get("params", ())
    row_limit = validated_query.get("row_limit")
    if not isinstance(row_limit, int) or row_limit < 1:
        raise QueryExecutionError("A positive row_limit is required for query execution.")
    user_limit = validated_query.get("user_limit", False)
    if not isinstance(user_limit, bool):
        raise QueryExecutionError("user_limit must be a boolean.")

    raw_rows = await _fetch_with_retries(sql, params, "Query")

    truncated = not user_limit and len(raw_rows) > row_limit

    tie_at_limit = False
    if validated_query.get("single_best_with_ties") is True:
        tie_at_limit = len(raw_rows) > 1
        raw_rows = raw_rows[:row_limit]
    else:
        raw_rows = raw_rows[:row_limit]

    no_data_reason = None
    matching_row_count = None
    if raw_rows and all(value is None for value in raw_rows[0]) and len(raw_rows) == 1:
        count_sql = validated_query.get("count_sql")
        count_params = validated_query.get("count_params", ())
        if count_sql is not None:
            if not isinstance(count_sql, str) or not isinstance(count_params, (tuple, list)):
                raise QueryExecutionError("Scalar aggregate diagnostic query is invalid.")
            count_rows = await _fetch_with_retries(
                count_sql,
                tuple(count_params),
                "Aggregate empty-result diagnostic",
            )
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