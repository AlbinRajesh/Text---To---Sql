"""Suggest existing text values for eligible filters in zero-row queries."""

import asyncio
import copy
import difflib
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from app.agent.nodes.safety_check import run_safety_check
from app.db.mysql_client import fetch_all_readonly

logger = logging.getLogger(__name__)

MAX_FILTERS_CHECKED = 3
MAX_DISTINCT_VALUES = 1000
MAX_SUGGESTIONS = 3
MIN_VALUE_LENGTH = 3
SIMILARITY_CUTOFF = 0.82
LOOKUP_TIMEOUT_SECONDS = 5.0
LOOKUP_TIMEOUT_MS = 4000

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEXT_TYPES = {
    "char",
    "varchar",
    "tinytext",
    "text",
    "mediumtext",
    "longtext",
    "enum",
    "set",
}
_SENSITIVE_COLUMN_PARTS = {
    "address",
    "birth",
    "client",
    "company",
    "customer",
    "email",
    "first",
    "firstname",
    "last",
    "lastname",
    "dob",
    "organization",
    "password",
    "phone",
    "secret",
    "ssn",
    "tenant",
    "token",
    "workspace",
}


def _normalise_value(value: str) -> str:
    """Normalize common Unicode, case, spacing, and punctuation differences."""
    text = unicodedata.normalize("NFKC", value).casefold()
    normalized = [
        " " if char.isspace() or unicodedata.category(char)[0] in {"P", "S", "Z"} else char
        for char in text
    ]
    return " ".join("".join(normalized).split())


def _conjunctive_filters(expression: Any) -> Optional[List[Dict[str, Any]]]:
    """Return leaves only when the full filter tree is an AND of predicates."""
    if not isinstance(expression, dict):
        return None

    boolean = expression.get("boolean")
    if boolean is None:
        return [expression]
    if boolean != "AND":
        return None

    conditions = expression.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return None

    leaves: List[Dict[str, Any]] = []
    for condition in conditions:
        nested = _conjunctive_filters(condition)
        if nested is None:
            return None
        leaves.extend(nested)
    return leaves


def _query_filters(query: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    expression = query.get("where_expression")
    if expression is not None:
        return _conjunctive_filters(expression)

    legacy_filters = query.get("where_conditions")
    if not isinstance(legacy_filters, list) or not legacy_filters:
        return None
    if len(legacy_filters) > 1 and query.get("combine_with", "AND") != "AND":
        return None
    if any(not isinstance(condition, dict) for condition in legacy_filters):
        return None
    return legacy_filters


def _sensitive_column(table: str, column: str) -> bool:
    if table.casefold() == "users":
        return True
    parts = set(re.split(r"[^a-z0-9]+", column.casefold()))
    return bool(parts & _SENSITIVE_COLUMN_PARTS)


def _candidate_filters(
    query: Dict[str, Any], schema_context: Dict[str, Any]
) -> List[Dict[str, str]]:
    filters = _query_filters(query)
    if filters is None:
        return []

    query_tables = [query.get("table")]
    joins = query.get("joins", [])
    if isinstance(joins, list):
        query_tables.extend(
            join.get("table")
            for join in joins
            if isinstance(join, dict) and isinstance(join.get("table"), str)
        )

    candidates: List[Dict[str, str]] = []
    seen = set()
    for condition in filters:
        if condition.get("operator") != "=":
            continue
        value = condition.get("value")
        column = condition.get("column")
        if (
            not isinstance(value, str)
            or len(value.strip()) < MIN_VALUE_LENGTH
            or not isinstance(column, str)
            or not _IDENTIFIER_PATTERN.fullmatch(column)
        ):
            continue

        table = condition.get("table")
        if table is None:
            owners = [
                candidate_table
                for candidate_table in query_tables
                if isinstance(candidate_table, str)
                and any(
                    isinstance(entry, dict) and entry.get("column") == column
                    for entry in schema_context.get(candidate_table, [])
                )
            ]
            if len(owners) != 1:
                continue
            table = owners[0]
        if (
            not isinstance(table, str)
            or table not in query_tables
            or not _IDENTIFIER_PATTERN.fullmatch(table)
        ):
            continue

        column_info = next(
            (
                entry
                for entry in schema_context.get(table, [])
                if isinstance(entry, dict) and entry.get("column") == column
            ),
            None,
        )
        if (
            not column_info
            or str(column_info.get("data_type", "")).casefold() not in _TEXT_TYPES
            or _sensitive_column(table, column)
        ):
            continue

        key = (table, column, value)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "table": table,
            "column": column,
            "operator": "=",
            "value": value,
        })
        if len(candidates) >= MAX_FILTERS_CHECKED:
            break

    return candidates


def _same_filter(left: Dict[str, Any], right: Dict[str, str]) -> bool:
    return all(
        left.get(key) == right[key]
        for key in ("table", "column", "operator", "value")
    )


def _remove_filter(
    expression: Any, target: Dict[str, str]
) -> Tuple[Any, bool]:
    if not isinstance(expression, dict):
        return expression, False
    if expression.get("boolean") is None:
        return (None, True) if _same_filter(expression, target) else (expression, False)

    conditions = expression.get("conditions")
    if expression.get("boolean") != "AND" or not isinstance(conditions, list):
        return expression, False

    remaining = []
    removed = False
    for condition in conditions:
        rewritten, matched = _remove_filter(condition, target)
        removed = removed or matched
        if rewritten is not None:
            remaining.append(rewritten)
    if not removed:
        return expression, False
    if not remaining:
        return None, True
    if len(remaining) == 1:
        return remaining[0], True
    return {**expression, "conditions": remaining}, True


def _replace_filter_value(
    expression: Any,
    target: Dict[str, str],
    replacement: str,
) -> Tuple[Any, int]:
    if not isinstance(expression, dict):
        return expression, 0
    if expression.get("boolean") is None:
        if _same_filter(expression, target):
            return {**expression, "value": replacement}, 1
        return expression, 0

    conditions = expression.get("conditions")
    if not isinstance(conditions, list):
        return expression, 0
    updated = []
    count = 0
    for condition in conditions:
        new_condition, matches = _replace_filter_value(
            condition, target, replacement
        )
        updated.append(new_condition)
        count += matches
    return {**expression, "conditions": updated}, count


def build_suggestion_execution_plan(
    query: Dict[str, Any],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]],
    suggestion: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Compile one user-confirmable choice through the normal safety checker."""
    target = {
        "table": suggestion["table"],
        "column": suggestion["column"],
        "operator": "=",
        "value": suggestion["original"],
    }
    corrected_query = copy.deepcopy(query)
    expression = corrected_query.get("where_expression")
    if expression is None:
        legacy_filters = corrected_query.get("where_conditions", [])
        if len(legacy_filters) == 1:
            expression = legacy_filters[0]
        elif legacy_filters:
            expression = {
                "boolean": corrected_query.get("combine_with", "AND"),
                "conditions": legacy_filters,
            }
    corrected_expression, replacements = _replace_filter_value(
        expression, target, suggestion["suggested"]
    )
    if replacements != 1:
        return None

    corrected_query.pop("where_conditions", None)
    corrected_query.pop("combine_with", None)
    corrected_query["where_expression"] = corrected_expression
    query_tables = [corrected_query["table"]]
    query_tables.extend(join["table"] for join in corrected_query.get("joins", []))
    safe_query = run_safety_check(
        corrected_query,
        valid_tables=query_tables,
        schema_context=schema_context,
        relationships=relationships,
    )
    return {
        "query": corrected_query,
        "safety_check_result": safe_query,
    }


def _similar_values(typed: str, existing: List[Any]) -> List[str]:
    typed_key = _normalise_value(typed)
    if len(typed_key) < MIN_VALUE_LENGTH:
        return []
    values_by_key: Dict[str, set[str]] = {}
    for value in existing:
        if not isinstance(value, str) or not value.strip():
            continue
        values_by_key.setdefault(_normalise_value(value), set()).add(value.strip())

    # Exclude normalized collisions: there is no safe single value to suggest.
    unique_values = {
        key: next(iter(values))
        for key, values in values_by_key.items()
        if len(values) == 1 and key
    }
    close_keys = difflib.get_close_matches(
        typed_key,
        sorted(unique_values),
        n=MAX_SUGGESTIONS + 1,
        cutoff=SIMILARITY_CUTOFF,
    )
    return [
        unique_values[key]
        for key in close_keys
        if unique_values[key] != typed.strip()
    ][:MAX_SUGGESTIONS]


async def _lookup_suggestions(
    query: Dict[str, Any],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]],
    candidate: Dict[str, str],
) -> List[str]:
    candidate_query = copy.deepcopy(query)
    expression = candidate_query.get("where_expression")
    if expression is None:
        legacy_filters = candidate_query.get("where_conditions", [])
        if len(legacy_filters) == 1:
            expression = legacy_filters[0]
        elif legacy_filters:
            expression = {
                "boolean": candidate_query.get("combine_with", "AND"),
                "conditions": legacy_filters,
            }
    reduced_expression, removed = _remove_filter(expression, candidate)
    if not removed:
        return []

    candidate_query.pop("where_conditions", None)
    candidate_query.pop("combine_with", None)
    candidate_query["where_expression"] = reduced_expression
    candidate_query["select_items"] = [{
        "table": candidate["table"],
        "column": candidate["column"],
        "aggregate": None,
        "distinct": False,
        "alias": "_suggestion_value",
    }]
    candidate_query["group_by"] = [{
        "table": candidate["table"],
        "column": candidate["column"],
    }]
    candidate_query["having_expression"] = None
    candidate_query["count_groups"] = False
    candidate_query["count_entity"] = None
    candidate_query["order_by"] = []
    candidate_query["limit"] = MAX_DISTINCT_VALUES

    query_tables = [candidate_query["table"]]
    query_tables.extend(join["table"] for join in candidate_query.get("joins", []))
    safe_query = run_safety_check(
        candidate_query,
        valid_tables=query_tables,
        schema_context=schema_context,
        relationships=relationships,
    )
    sql = safe_query["sql"]
    timeout_hint = f"MAX_EXECUTION_TIME({LOOKUP_TIMEOUT_MS})"
    sql, replacements = re.subn(
        r"MAX_EXECUTION_TIME\(\d+\)",
        timeout_hint,
        sql,
        count=1,
    )
    if replacements != 1:
        raise ValueError("Safe suggestion query is missing its execution-time limit.")

    rows = await fetch_all_readonly(sql, tuple(safe_query["params"]))
    return _similar_values(
        candidate["value"],
        [row[0] for row in rows[:MAX_DISTINCT_VALUES] if row],
    )


async def find_value_suggestions(
    query: Dict[str, Any],
    schema_context: Dict[str, Any],
    relationships: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """Return suggestions for safe text equality filters, without rerunning SQL."""
    try:
        candidates = _candidate_filters(query, schema_context)
    except Exception:
        logger.exception("did_you_mean: could not inspect validated filters")
        return []
    if not candidates:
        return []

    suggestions: List[Dict[str, str]] = []
    try:
        async with asyncio.timeout(LOOKUP_TIMEOUT_SECONDS):
            for candidate in candidates:
                try:
                    values = await _lookup_suggestions(
                        query,
                        schema_context,
                        relationships or [],
                        candidate,
                    )
                except Exception:
                    logger.exception(
                        "did_you_mean: lookup failed for %s.%s",
                        candidate["table"],
                        candidate["column"],
                    )
                    continue
                suggestions.extend(
                    {
                        "table": candidate["table"],
                        "column": candidate["column"],
                        "original": candidate["value"],
                        "suggested": value,
                    }
                    for value in values
                )
                if len(suggestions) >= MAX_SUGGESTIONS:
                    return suggestions[:MAX_SUGGESTIONS]
    except TimeoutError:
        logger.warning("did_you_mean: total lookup budget exceeded")

    return suggestions
