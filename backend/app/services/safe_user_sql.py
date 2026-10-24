"""Validation and bounded execution for user-confirmed read-only SQL."""

import re
from typing import Any, Dict, List, Tuple

import sqlglot
from fastapi.encoders import jsonable_encoder
from sqlglot import exp

from app.agent.nodes.blocked_tables import find_blocked_tables
from app.agent.nodes.safety_check import (
    PERSONAL_RESULT_COLUMNS,
    SENSITIVE_RESULT_COLUMNS,
    _personal_column_use_allowed,
)

MAX_USER_SQL_TABLES = 5
MAX_USER_SQL_ROWS = 200
MAX_USER_SQL_LENGTH = 12_000
SAFE_FUNCTIONS = {
    "ABS", "AVG", "CAST", "CEIL", "CEILING", "COALESCE", "CONCAT",
    "COUNT", "DATE", "DATE_FORMAT", "DAY", "FLOOR", "HOUR", "IF", "IFNULL",
    "LEFT", "LENGTH", "LOWER", "LPAD", "MAX", "MIN", "MONTH",
    "NULLIF", "ROUND", "REPLACE", "RIGHT", "ROUND", "SUBSTRING",
    "SUM", "TIME", "TRIM", "UPPER", "YEAR",
}
_TRAILING_RUN_REQUEST = re.compile(
    r";\s*(?:--\s*)?(?:[-\u2013\u2014]\s*)?"
    r"(?:can\s+you\s+)?(?:please\s+)?run\s+(?:this|it)"
    r"(?:\s+sql(?:\s+query)?)?[.!]?\s*$",
    re.IGNORECASE,
)


class UserSQLValidationError(ValueError):
    """Raised when pasted SQL is not an allowed bounded read-only query."""


def _normalize_input(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise UserSQLValidationError("Paste a read-only SELECT query.")
    value = sql.strip()
    if len(value) > MAX_USER_SQL_LENGTH:
        raise UserSQLValidationError("The pasted query is too long.")
    value = _TRAILING_RUN_REQUEST.sub(";", value)
    if re.search(r"--|/\*|\*/|^\s*#", value, re.MULTILINE):
        raise UserSQLValidationError("SQL comments are not allowed in pasted queries.")
    return value


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return str(function.name).upper()
    return str(function.sql_name()).upper()


def _column_owner(
    column: exp.Column,
    aliases: Dict[str, str],
    schema: Dict[str, List[Dict[str, Any]]],
    query_tables: set[str],
) -> Tuple[str, str]:
    if column.db or column.catalog or column.name == "*":
        raise UserSQLValidationError(
            "Use explicit columns from the current database; wildcard and cross-database references are not allowed."
        )
    if column.table:
        owner = aliases.get(column.table.lower())
        if owner is None:
            raise UserSQLValidationError(
                f"Column qualifier {column.table!r} does not match a query table."
            )
        candidates = [owner]
    else:
        candidates = [
            table
            for table in query_tables
            for columns in [schema[table]]
            if any(
                isinstance(item, dict)
                and str(item.get("column", "")).lower() == column.name.lower()
                for item in columns
            )
        ]
    owners = [
        table
        for table in candidates
        if any(
            isinstance(item, dict)
            and str(item.get("column", "")).lower() == column.name.lower()
            for item in schema.get(table, [])
        )
    ]
    if len(owners) != 1:
        raise UserSQLValidationError(
            f"Column {column.name!r} is unknown or ambiguous; qualify it with a query-table alias."
        )
    return owners[0], column.name.lower()


def validate_user_select(sql: str, schema: Dict[str, List[Dict[str, Any]]]) -> str:
    """Accept one SELECT over known tables/columns and clamp its result size."""
    normalized = _normalize_input(sql)
    try:
        parsed = [statement for statement in sqlglot.parse(normalized, read="mysql") if statement is not None]
    except sqlglot.errors.ParseError as exc:
        raise UserSQLValidationError(f"Could not parse the pasted SQL: {exc}") from exc
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
        raise UserSQLValidationError("Only one read-only SELECT statement can be confirmed.")

    tree = parsed[0]
    if tree.find(exp.With) or len(list(tree.find_all(exp.Select))) != 1:
        raise UserSQLValidationError("CTEs and nested queries are not supported for pasted SQL.")
    if tree.args.get("into") or tree.args.get("locks"):
        raise UserSQLValidationError("SELECT INTO and locking clauses are not allowed.")
    if any(
        tree.find(expression_type) is not None
        for expression_type in (
            exp.CurrentUser,
            exp.CurrentRole,
            exp.CurrentCatalog,
            exp.CurrentDatabase,
            exp.CurrentSchema,
            exp.SessionParameter,
            exp.Parameter,
        )
    ):
        raise UserSQLValidationError(
            "Database session details and user variables cannot be queried."
        )
    if not tree.expressions or any(
        isinstance(item, exp.Star)
        or (isinstance(item, exp.Column) and item.name == "*")
        or (
            isinstance(item, exp.Alias)
            and isinstance(item.this, exp.Column)
            and item.this.name == "*"
        )
        for item in tree.expressions
    ):
        raise UserSQLValidationError(
            "Select explicit columns; wildcard result selection is not allowed."
        )

    tables = list(tree.find_all(exp.Table))
    schema_names = {name.casefold(): name for name in schema}
    table_names = {
        schema_names.get(table.name.casefold(), table.name)
        for table in tables
    }
    if not tables or len(table_names) > MAX_USER_SQL_TABLES:
        raise UserSQLValidationError(
            f"Use between one and {MAX_USER_SQL_TABLES} current-database tables."
        )
    if any(table.db or table.catalog for table in tables):
        raise UserSQLValidationError("Cross-database table references are not allowed.")
    unknown_tables = table_names - set(schema)
    blocked_tables = set(find_blocked_tables(table_names))
    if unknown_tables or blocked_tables:
        raise UserSQLValidationError("The query references a table that is not available.")

    aliases: Dict[str, str] = {}
    for table in tables:
        canonical_name = schema_names.get(table.name.casefold())
        if canonical_name is None:
            continue
        alias = table.alias_or_name.casefold()
        if alias in aliases and aliases[alias] != canonical_name:
            raise UserSQLValidationError("Each table alias must resolve to one table.")
        aliases[alias] = canonical_name
        aliases[canonical_name.casefold()] = canonical_name

    output_aliases = {
        expression.alias.casefold()
        for expression in tree.expressions
        if isinstance(expression, exp.Alias)
    }
    for alias in output_aliases:
        if sum(
            isinstance(expression, exp.Alias)
            and expression.alias.casefold() == alias
            for expression in tree.expressions
        ) > 1:
            raise UserSQLValidationError("Use unique names for selected column aliases.")

    for function in tree.find_all(exp.Func):
        if isinstance(function, (exp.Binary, exp.Unary)):
            continue
        name = _function_name(function)
        if name not in SAFE_FUNCTIONS:
            raise UserSQLValidationError(f"SQL function {name!r} is not allowed.")
        if name in {"HOUR", "TIME"}:
            argument = function.this
            if not isinstance(argument, exp.Column):
                raise UserSQLValidationError(
                    f"{name} must be applied directly to a verified date/time column."
                )
            owner, column_name = _column_owner(
                argument, aliases, schema, table_names
            )
            column_info = next(
                (
                    item
                    for item in schema.get(owner, [])
                    if isinstance(item, dict)
                    and str(item.get("column", "")).casefold() == column_name
                ),
                None,
            )
            data_type = (
                str(column_info.get("data_type", "")).casefold()
                if isinstance(column_info, dict)
                else ""
            )
            if data_type not in {"date", "datetime", "timestamp", "time"}:
                raise UserSQLValidationError(
                    f"{name} requires a verified date/time column."
                )
    for column in tree.find_all(exp.Column):
        if (
            not column.table
            and column.name.casefold() in output_aliases
            and column.find_ancestor(exp.Order, exp.Group, exp.Having) is not None
        ):
            continue
        owner, name = _column_owner(column, aliases, schema, table_names)
        if name in SENSITIVE_RESULT_COLUMNS:
            raise UserSQLValidationError("Sensitive credential columns cannot be queried.")
        if (owner, name) in PERSONAL_RESULT_COLUMNS and not _personal_column_use_allowed(column):
            raise UserSQLValidationError(
                "Personal contact, address, or location columns cannot be selected or filtered."
            )

    limit = tree.args.get("limit")
    if limit is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(MAX_USER_SQL_ROWS + 1)))
    else:
        limit_value = limit.expression
        if not isinstance(limit_value, exp.Literal) or not limit_value.is_int:
            raise UserSQLValidationError("The SELECT limit must be a positive integer literal.")
        if int(limit_value.this) < 1:
            raise UserSQLValidationError("The SELECT limit must be a positive integer literal.")
        tree.set(
            "limit",
            exp.Limit(
                expression=exp.Literal.number(
                    min(int(limit_value.this), MAX_USER_SQL_ROWS + 1)
                )
            ),
        )
    return tree.sql(dialect="mysql")


async def execute_user_select(
    sql: str,
    schema: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    validated_sql = validate_user_select(sql, schema)
    from app.db.mysql_client import fetch_readonly_result

    columns, rows = await fetch_readonly_result(validated_sql)
    truncated = len(rows) > MAX_USER_SQL_ROWS
    visible_rows = rows[:MAX_USER_SQL_ROWS]
    return {
        "sql": validated_sql,
        "columns": columns,
        "results": [
            jsonable_encoder({column: value for column, value in zip(columns, row)})
            for row in visible_rows
        ],
        "count": len(visible_rows),
        "row_count": len(visible_rows),
        "truncated": truncated,
        "row_limit": MAX_USER_SQL_ROWS,
    }
