"""
grade_sample.py  (Node 4: Grade the Sample)

Checks if the cached schema (from schema_grounding.py) has enough
information to answer the user's question. If a needed table/column
seems missing, it widens the vector search to pull more tables and retries.

This node uses a fast/cheap LLM call to decide "does this schema look
sufficient for this question". If insufficient after MAX_RETRIES, raises
so the caller can route to Node 11 (human escalation).
"""

import logging
import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from app.agent.nodes.schema_grounding import (
    get_cached_schema,
    get_cached_relationships,
    refresh_schema_cache,
    SchemaGroundingError,
)
from app.services.embedding_service import retrieve_relevant_tables

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
MAX_SCHEMA_TABLES = 24
_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_USER_SUBSCRIPTION_INTENT = re.compile(
    r"\busers?\b.{0,100}\bsubscriptions?\b.{0,30}\bactive\b"
    r"|\busers?\b.{0,100}\bactive\s+subscriptions?\b",
    re.IGNORECASE,
)


class GradeSampleError(Exception):
    """Raised when the schema cache cannot be made sufficient after retries."""
    pass

class SchemaGraderUnavailableError(GradeSampleError):
    """Raised when the grader LLM call itself fails (rate limit, network, bad JSON).

    This is different from "schema not sufficient". Widening the table search
    cannot fix an unavailable or misbehaving LLM, so we stop immediately.
    """
    pass


class MissingRelationshipError(Exception):
    """Raised when the question needs a join between two real tables that the
    full verified relationship catalog cannot connect.

    Deliberately NOT a GradeSampleError: this is an honest "cannot answer",
    not an escalation. Widening the table search cannot create a relationship.
    """

    def __init__(self, tables: Tuple[str, str], detail: str = "") -> None:
        self.tables = tables
        self.detail = detail
        # User-facing text: no table names or internals.
        self.reasoning = (
            "I can't answer this reliably yet because the data needed to connect "
            "these two kinds of information is not set up as a verified relationship."
        )
        super().__init__(
            f"No verified relationship path between {tables[0]} and {tables[1]}"
            + (f": {detail}" if detail else "")
        )


_MISSING_TYPES = {"table", "column", "relationship"}


def _normalize_missing_items(raw: List[Any]) -> List[Dict[str, Any]]:
    """Accepts plain strings or typed dicts. Unknown types become untyped."""
    items: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            item_type = entry.get("type")
            tables = entry.get("tables")
            items.append({
                "type": item_type if item_type in _MISSING_TYPES else None,
                "detail": str(entry.get("detail") or entry),
                "tables": [t for t in tables if isinstance(t, str)]
                if isinstance(tables, list)
                else [],
            })
        else:
            items.append({"type": None, "detail": str(entry), "tables": []})
    return items


def _catalog_has_path(
    relationships: List[Dict[str, Any]], start: str, goal: str
) -> bool:
    """True if the verified catalog connects two tables at ANY number of hops."""
    neighbors: Dict[str, set] = {}
    for edge in relationships:
        left, right = edge.get("table"), edge.get("referenced_table")
        if isinstance(left, str) and isinstance(right, str):
            neighbors.setdefault(left, set()).add(right)
            neighbors.setdefault(right, set()).add(left)
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == goal:
            return True
        for neighbor in neighbors.get(current, ()):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return False


def _find_disconnected_pair(
    items: List[Dict[str, Any]],
    schema: Dict[str, Any],
    relationships: List[Dict[str, Any]],
) -> Optional[Tuple[Tuple[str, str], str]]:
    """Only a typed 'relationship' item naming exactly two real tables that the
    FULL catalog cannot connect counts. Everything else returns None."""
    for item in items:
        if item["type"] != "relationship":
            continue
        tables = list(dict.fromkeys(item["tables"]))
        if len(tables) != 2:
            continue
        first, second = tables
        if first not in schema or second not in schema:
            continue
        if not _catalog_has_path(relationships, first, second):
            return (first, second), item["detail"]
    return None


def _flatten_schema_for_prompt(schema: Dict[str, List[Dict[str, Any]]]) -> str:
    """
    Turns the cached schema dict into a compact text block for the
    cheap LLM call to reason over (table -> column names only, no
    need to send full type info for a relevance check).
    """
    lines = []
    for table, columns in schema.items():
        # Ensure we handle different schema structures gracefully
        if isinstance(columns, list) and len(columns) > 0 and isinstance(columns[0], dict):
            col_names = ", ".join(c.get("column", str(c)) for c in columns)
        elif isinstance(columns, list):
            col_names = ", ".join(str(c) for c in columns)
        else:
            col_names = str(columns)
            
        lines.append(f"{table}: {col_names}")
    return "\n".join(lines)


def _expand_schema_for_relationships(
    schema: Dict[str, List[Dict[str, Any]]],
    relevant_tables: List[str],
    relationships: List[Dict[str, Any]],
    question: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Add directly related join candidates without sending the whole schema."""
    selected = list(dict.fromkeys(table for table in relevant_tables if table in schema))
    selected_set = set(selected)
    distances = {}
    frontier = [(table, 0) for table in selected]
    while frontier:
        current, distance = frontier.pop(0)
        if distance == 4:
            continue
        for relationship in relationships:
            endpoints = (relationship["table"], relationship["referenced_table"])
            if current not in endpoints:
                continue
            neighbor = endpoints[1] if endpoints[0] == current else endpoints[0]
            if neighbor not in schema or neighbor in selected_set:
                continue
            next_distance = distance + 1
            if neighbor not in distances or next_distance < distances[neighbor]:
                distances[neighbor] = next_distance
                frontier.append((neighbor, next_distance))

    question_terms = set(_WORD_PATTERN.findall(question.lower()))

    def relevance(table: str) -> Tuple[int, int, str]:
        table_terms = set(_WORD_PATTERN.findall(table.lower().replace("_", " ")))
        column_terms = {
            term
            for item in schema[table]
            for term in _WORD_PATTERN.findall(str(item.get("column", "")).lower().replace("_", " "))
        }
        return (
            -len(question_terms & (table_terms | column_terms)),
            distances[table],
            table,
        )

    available = max(0, MAX_SCHEMA_TABLES - len(selected))
    expanded = sorted(distances, key=relevance)[:available]
    selected.extend(table for table in expanded if table not in selected_set)
    selected_set = set(selected)

    filtered_schema = {table: schema[table] for table in selected}
    filtered_relationships = [
        relationship
        for relationship in relationships
        if relationship["table"] in selected_set
        and relationship["referenced_table"] in selected_set
    ]
    return filtered_schema, filtered_relationships


async def _ask_cheap_model_if_sufficient(
    question: str, schema_text: str, relationships: List[Dict[str, Any]]
) -> Tuple[bool, List[Any]]:
    from app.services.llm_service import get_llm_service

    prompt = (
        "You are checking if a database schema has enough information to "
        "answer a question. Respond ONLY in JSON: "
        '{"sufficient": true/false, "missing": [{"type": "table" | "column" | '
        '"relationship", "detail": "short description", "tables": ["table_name"]}]}\n\n'
        f"Question: {question}\n\n"
        f"Available schema (table: columns):\n{schema_text}\n\n"
        "Verified table relationships (only these may be joined):\n"
        f"{relationships}\n\n"
        "Judge sufficiency only against exact tables, columns, and verified "
        "relationships shown above. Do not invent a column or relationship "
        "because a synonym seems likely (for example, do not require a "
        "column named 'booking_value' when a plausible amount column is "
        "present). Report a missing item only when the required exact schema "
        "element is absent; if the natural-language term is ambiguous, mark "
        "the schema sufficient and leave semantic interpretation to query "
        "generation. For a missing relationship, set type to 'relationship' and "
        "'tables' to exactly the two exact table names from the schema above that "
        "would need to be joined. For a missing column, set type to 'column' and "
        "'tables' to the table that should hold it. Use a plain string only if "
        "you cannot classify the item."
    )

    llm = get_llm_service()
    try:
        parsed = await llm.call_agent_json("SCHEMA_GRADER", [{"role": "user", "content": prompt}], temperature=0.0)
    except Exception as e:
        logger.error("grade_sample LLM call failed: %s", e)
        raise SchemaGraderUnavailableError(
            f"Schema grader LLM call failed: {e}"
        ) from e

    if not isinstance(parsed, dict) or "sufficient" not in parsed:
        raise SchemaGraderUnavailableError(
            f"Schema grader returned an unexpected response: {parsed!r}"
        )
    missing = parsed.get("missing") or []
    if not isinstance(missing, list):
        missing = [str(missing)]
    return bool(parsed["sufficient"]), [
        item if isinstance(item, dict) else str(item) for item in missing
    ]


async def grade_schema_sufficiency(question: str) -> Dict[str, Any]:
    """
    Main entry point for Node 4.

    1. Reads the current cached schema (Node 3's cache).
    2. Uses vector embeddings to pull the top K relevant tables for the question.
    3. Asks the cheap model if the filtered schema is sufficient for the question.
    4. If not sufficient, widens the top K search net and loops.
    5. Capped at MAX_RETRIES. Raises GradeSampleError if still
       insufficient after all retries -> caller routes to Node 11.

    Raises MissingRelationshipError when the grader names two real tables that
    the full verified catalog cannot connect (a refusal, not an escalation).
    Raises SchemaGraderUnavailableError if the grader call itself fails, and
    GradeSampleError if the schema is still insufficient after MAX_RETRIES.
    """
    attempts = 0
    last_missing: List[str] = []

    while attempts < MAX_RETRIES:
        attempts += 1

        try:
            full_schema = get_cached_schema()
            all_relationships = get_cached_relationships()
        except SchemaGroundingError:
            # Cache was never built (e.g. startup hook failed) — force a build now.
            logger.warning("Schema cache empty on attempt %d, forcing refresh", attempts)
            await refresh_schema_cache()
            full_schema = get_cached_schema()
            all_relationships = get_cached_relationships()

        # Widen the retrieval net each retry (6, 12, 18 tables) instead of
        # re-hitting MySQL — schema structure rarely changes, so the real
        # fix for "insufficient" is usually "wrong tables were retrieved".
        top_k = 6 * attempts
        relevant_tables = await retrieve_relevant_tables(question, top_k=top_k)
        filtered_schema, relationships = _expand_schema_for_relationships(
            full_schema, relevant_tables, all_relationships, question
        )

        if _USER_SUBSCRIPTION_INTENT.search(question) and not any(
            {edge.get("table"), edge.get("referenced_table")}
            == {"users", "subscriptions"}
            for edge in all_relationships
        ):
            logger.info(
                "Schema grading found no verified users-to-subscriptions "
                "relationship; passing the available schema to query generation "
                "for a safe refusal."
            )
            return {
                "sufficient": True,
                "schema": filtered_schema,
                "relationships": relationships,
                "attempts": attempts,
            }

        schema_text = _flatten_schema_for_prompt(filtered_schema)
        sufficient, missing = await _ask_cheap_model_if_sufficient(
            question, schema_text, relationships
        )

        if sufficient:
            logger.info("Schema graded sufficient on attempt %d (%d tables)", attempts, len(filtered_schema))
            return {
                "sufficient": True,
                "schema": filtered_schema,   # only the relevant tables go to Node 6 now
                "relationships": relationships,
                "attempts": attempts,
            }

        missing_items = _normalize_missing_items(missing)
        disconnected = _find_disconnected_pair(
            missing_items, full_schema, all_relationships
        )
        if disconnected is not None:
            pair, detail = disconnected
            logger.info(
                "Schema grading: no verified relationship path between %s and %s "
                "in the full catalog; refusing instead of widening.",
                pair[0], pair[1],
            )
            raise MissingRelationshipError(pair, detail)

        last_missing = [item["detail"] for item in missing_items]
        logger.info(
            "Insufficient on attempt %d, widening search. Missing: %s",
            attempts, last_missing,
        )

    # Exhausted retries without success -> Node 11 (human escalation) territory.
    raise GradeSampleError(
        f"Schema not sufficient for question after {MAX_RETRIES} attempts. "
        f"Last known missing items: {last_missing}"
    )