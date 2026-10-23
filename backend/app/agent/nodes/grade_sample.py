"""
grade_sample.py  (Node 4: Grade the Sample)

Checks if the cached schema (from schema_grounding.py) has enough
information to answer the user's question. If a needed table/column
seems missing, it widens the vector search to pull more tables and retries.
An explicitly missing relationship between verified tables refuses immediately.

This node uses a fast/cheap LLM call to decide "does this schema look
sufficient for this question". If insufficient after MAX_RETRIES, raises
so the caller can route to Node 11 (human escalation).
"""

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from app.agent.nodes.calculated_metrics import METRIC_REGISTRY
from app.agent.nodes.schema_grounding import (
    get_cached_schema,
    get_cached_relationships,
    refresh_schema_cache,
    SchemaGroundingError,
)
from app.agent.search_config import SEARCHABLE_COLUMNS, SEARCH_ROOT_TABLES
from app.services.embedding_service import retrieve_relevant_tables
from app.services.errors import infrastructure_failure_code

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
MAX_SCHEMA_TABLES = 24
_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_ACTIVE_SCHEMA_DIAGNOSTICS: ContextVar[Dict[str, Any] | None] = ContextVar(
    "active_schema_diagnostics", default=None
)


@contextmanager
def schema_diagnostics_scope(diagnostics: Dict[str, Any]):
    token = _ACTIVE_SCHEMA_DIAGNOSTICS.set(diagnostics)
    try:
        yield
    finally:
        _ACTIVE_SCHEMA_DIAGNOSTICS.reset(token)


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
        self.reason_code = "MISSING_VERIFIED_RELATIONSHIP"
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


def _is_irrelevant_active_offer_view_item(
    item: Dict[str, Any],
    active_offer_rules: List[Dict[str, Any]],
) -> bool:
    if not any(
        rule.get("term") == "active_offer_status"
        for rule in active_offer_rules
    ):
        return False
    if any(
        table.casefold() == "editable_offers_view"
        for table in item.get("tables", [])
    ):
        return True
    return "editable_offers_view" in item.get("detail", "").casefold()


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


def _add_configured_search_context(
    schema: Dict[str, List[Dict[str, Any]]],
    full_schema: Dict[str, List[Dict[str, Any]]],
    relationships: List[Dict[str, Any]],
    all_relationships: List[Dict[str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Add the small, verified schema slice needed by the typed search compiler."""
    search_tables = list(SEARCH_ROOT_TABLES.values())
    search_tables.extend(
        field["table"]
        for fields in SEARCHABLE_COLUMNS.values()
        for field in fields
    )
    search_tables = list(dict.fromkeys(
        table for table in search_tables if table in full_schema
    ))
    required_columns: Dict[str, set[str]] = {
        table: set() for table in search_tables
    }
    configured_edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for table in search_tables:
        required_columns[table].update(
            item["column"]
            for item in full_schema[table]
            if isinstance(item, dict) and item.get("key") == "PRI"
        )

    for scope, fields in SEARCHABLE_COLUMNS.items():
        root_table = SEARCH_ROOT_TABLES[scope]
        if root_table not in full_schema:
            continue
        for field in fields:
            table = field["table"]
            column = field["column"]
            if table not in full_schema or not any(
                item.get("column") == column for item in full_schema[table]
                if isinstance(item, dict)
            ):
                continue
            required_columns.setdefault(table, set()).add(column)
            relationship_ref = field.get("relationship")
            if relationship_ref is None:
                continue
            relation_key = (
                relationship_ref["constraint_name"],
                relationship_ref["table"],
                relationship_ref["referenced_table"],
            )
            edge = next(
                (
                    item for item in all_relationships
                    if (
                        item.get("constraint_name"),
                        item.get("table"),
                        item.get("referenced_table"),
                    ) == relation_key
                ),
                None,
            )
            if edge is None:
                continue
            configured_edges[relation_key] = edge
            for pair in edge.get("column_pairs", []):
                required_columns.setdefault(edge["table"], set()).add(pair["column"])
                required_columns.setdefault(edge["referenced_table"], set()).add(
                    pair["referenced_column"]
                )

    if "shop_details" in full_schema and any(
        item.get("column") == "status"
        for item in full_schema["shop_details"]
        if isinstance(item, dict)
    ):
        required_columns.setdefault("shop_details", set()).add("status")

    selected_tables = list(schema)
    selected_tables = [table for table in selected_tables if table not in search_tables]
    selected_tables.extend(search_tables)
    selected_set = set(selected_tables)

    augmented_schema: Dict[str, List[Dict[str, Any]]] = {}
    for table in selected_tables:
        existing = schema.get(table, [])
        existing_columns = {
            item.get("column") for item in existing if isinstance(item, dict)
        }
        added = [
            item
            for item in full_schema.get(table, [])
            if isinstance(item, dict)
            and item.get("column") in required_columns.get(table, set())
            and item.get("column") not in existing_columns
        ]
        augmented_schema[table] = [*existing, *added]

    augmented_relationships = [
        edge for edge in relationships
        if edge.get("table") in selected_set
        and edge.get("referenced_table") in selected_set
    ]
    existing_keys = {
        (
            edge.get("constraint_name"),
            edge.get("table"),
            edge.get("referenced_table"),
        )
        for edge in augmented_relationships
    }
    for key, edge in configured_edges.items():
        if key not in existing_keys and edge["table"] in selected_set and edge[
            "referenced_table"
        ] in selected_set:
            augmented_relationships.append(edge)

    return augmented_schema, augmented_relationships


def _build_schema_grader_prompt(
    original_question: str,
    rewritten_question: str,
    schema_text: str,
    relationships: List[Dict[str, Any]],
    *,
    include_configured_search_guidance: bool = True,
    available_calculated_metrics: Dict[str, List[str]] | None = None,
    matched_business_rules: List[Dict[str, Any]] | None = None,
) -> str:
    configured_search_fields = "\n".join(
        f"{scope}: " + ", ".join(
            f"{field['table']}.{field['column']}" for field in fields
        )
        for scope, fields in SEARCHABLE_COLUMNS.items()
    )
    search_guidance = (
        "Configured text-search fields:\n"
        f"{configured_search_fields}\n\n"
        "A text search over these configured fields is sufficient to find shops "
        "by a term; do not require a product table, a category link, or any other "
        "relationship for that search. For every other question, use only "
        "relationships verified in the catalog below.\n\n"
    )
    calculated_metric_guidance = ""
    if available_calculated_metrics:
        metric_lines = "\n".join(
            f"- {name} (also called: {', '.join(aliases)})"
            for name, aliases in sorted(available_calculated_metrics.items())
        )
        calculated_metric_guidance = (
            "Registered calculated metrics available because their business "
            "rules matched:\n"
            f"{metric_lines}\n"
            "These are code-defined values, not physical schema columns. Do "
            "not report a matched metric or its listed synonym as a missing "
            "column; query generation resolves it using verified source "
            "columns.\n\n"
        )
    active_offer_guidance = ""
    matched_rule_guidance = ""
    if matched_business_rules:
        rule_lines = [
            f"- {rule.get('term')}: {rule.get('definition')}"
            for rule in matched_business_rules
            if isinstance(rule, dict)
            and isinstance(rule.get("term"), str)
            and isinstance(rule.get("definition"), str)
        ]
        if rule_lines:
            matched_rule_guidance = (
                "Matched business rules define the intended meaning and data "
                "path for this request. Do not require alternative tables or "
                "relationships that contradict these definitions:\n"
                + "\n".join(rule_lines)
                + "\n\n"
            )
    for rule in matched_business_rules or []:
        if rule.get("term") == "active_offer_status":
            active_offer_guidance = (
                "Matched business rule for active offers:\n"
                f"{rule.get('definition', '')}\n"
                "For active-offer questions that also filter by shop city, "
                "assess the verified path offer -> shop_details -> "
                "shop_address. Do not require a direct relationship from "
                "editable_offers_view to shop_address; use the canonical "
                "offer table for active offers. Only use edges shown in the "
                "verified relationship catalog.\n\n"
            )
            break
    return (
        "You are checking if a database schema has enough information to "
        "answer a question. Respond ONLY in JSON: "
        '{"sufficient": true/false, "missing": [{"type": "table" | "column" | '
        '"relationship", "detail": "short description", "tables": ["table_name"]}]}\n\n'
        "Original user question (the authority for requested data and meaning):\n"
        f"{original_question}\n\n"
        "Classifier rewrite (secondary context only; it may contain mistakes and "
        "must not add required fields or conditions):\n"
        f"{rewritten_question}\n\n"
        f"Available schema (table: columns):\n{schema_text}\n\n"
        f"{calculated_metric_guidance}"
        f"{matched_rule_guidance}"
        f"{active_offer_guidance}"
        f"{search_guidance if include_configured_search_guidance else ''}"
        "Verified table relationships (only these may be joined):\n"
        f"{relationships}\n\n"
        "The schema may include a bounded, preconfigured text-search scope and "
        "its verified relationships; treat those fields as available only when "
        "shown above. "
        "Judge sufficiency only against exact tables, columns, and verified "
        "relationships shown above. Do not invent a column or relationship "
        "based on a synonym when a plausible matching schema element is "
        "present. Report a missing item only when the required exact schema "
        "element is absent; if the natural-language term is ambiguous, mark "
        "the schema sufficient and leave semantic interpretation to query "
        "generation. For a missing relationship, set type to 'relationship' and "
        "'tables' to exactly the two exact table names from the schema above that "
        "would need to be joined. For a missing column, set type to 'column' and "
        "'tables' to the table that should hold it. Use a plain string only if "
        "you cannot classify the item."
    )


async def _ask_cheap_model_if_sufficient(
    original_question: str,
    rewritten_question: str,
    schema_text: str,
    relationships: List[Dict[str, Any]],
    available_calculated_metrics: Dict[str, List[str]] | None = None,
    matched_business_rules: List[Dict[str, Any]] | None = None,
) -> Tuple[bool, List[Any]]:
    from app.services.llm_service import get_llm_service

    if matched_business_rules is None:
        matched_business_rules = _matched_business_rules(original_question)
    prompt = _build_schema_grader_prompt(
        original_question,
        rewritten_question,
        schema_text,
        relationships,
        available_calculated_metrics=available_calculated_metrics,
        matched_business_rules=matched_business_rules,
    )

    llm = get_llm_service()
    raw_output: str | None = None
    try:
        call_with_raw = getattr(llm, "call_agent_json_with_raw", None)
        if callable(call_with_raw):
            parsed, raw_output = await call_with_raw(
                "SCHEMA_GRADER",
                [{"role": "user", "content": prompt}],
                temperature=0.0,
            )
        else:
            parsed = await llm.call_agent_json(
                "SCHEMA_GRADER",
                [{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            raw_output = json.dumps(parsed, ensure_ascii=False, default=str)
    except Exception as e:
        diagnostics = _ACTIVE_SCHEMA_DIAGNOSTICS.get()
        if diagnostics is not None and diagnostics.get("schema_attempts"):
            diagnostics["schema_attempts"][-1]["grader_error"] = (
                f"{type(e).__name__}: {e}"
            )
            raw_error_output = getattr(e, "raw_output", None)
            if isinstance(raw_error_output, str):
                diagnostics["schema_attempts"][-1]["grader_raw_output"] = (
                    raw_error_output
                )
        code = infrastructure_failure_code(e)
        if code and diagnostics is not None:
            diagnostics["infrastructure_errors"].append({
                "code": code,
                "stage": "grade_schema",
                "detail": str(e),
            })
        logger.error("grade_sample LLM call failed: %s", e)
        raise SchemaGraderUnavailableError(
            f"Schema grader LLM call failed: {e}"
        ) from e

    diagnostics = _ACTIVE_SCHEMA_DIAGNOSTICS.get()
    if diagnostics is not None and diagnostics.get("schema_attempts"):
        diagnostics["schema_attempts"][-1]["grader_raw_output"] = raw_output

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


def _matched_business_rules(question):
    """Business rules matching the question, or None if the lookup failed."""
    try:
        from app.agent.nodes.business_rules import find_relevant_rules

        return find_relevant_rules(question)
    except Exception as exc:
        logger.warning(
            "Business rule lookup failed; using the normal schema check: %s", exc
        )
        return None


def _matched_calculated_metrics(
    rules: List[Dict[str, Any]] | None,
) -> Dict[str, List[str]]:
    """Return registry metrics whose named business rule matched the question."""
    available: Dict[str, List[str]] = {}
    for rule in rules or []:
        metric_name = rule.get("term")
        if metric_name not in METRIC_REGISTRY:
            continue
        synonyms = rule.get("synonyms", [])
        aliases = [
            alias for alias in synonyms
            if isinstance(alias, str) and alias.strip()
        ]
        available[metric_name] = list(dict.fromkeys([metric_name, *aliases]))
    return available


def _metric_for_missing_column(
    item: Dict[str, Any],
    available_metrics: Dict[str, List[str]],
) -> str | None:
    if item.get("type") not in {None, "column"}:
        return None
    detail = re.sub(r"[^a-z0-9]+", " ", item.get("detail", "").casefold()).strip()
    tables = item.get("tables", [])
    for metric_name, aliases in available_metrics.items():
        metric_table = METRIC_REGISTRY[metric_name].table
        if tables and metric_table not in tables:
            continue
        for alias in aliases:
            normalized_alias = re.sub(
                r"[^a-z0-9]+", " ", alias.casefold()
            ).strip()
            match = (
                re.search(
                    rf"(?:^| ){re.escape(normalized_alias)}(?: |$)", detail
                )
                if normalized_alias
                else None
            )
            if match:
                residual = f"{detail[:match.start()]} {detail[match.end():]}"
                residual_words = set(residual.split())
                if residual_words <= {
                    "missing", "column", "columns", "for", "of", "the",
                    "a", "an", "offer", "offers", "field", "on", "in",
                }:
                    return metric_name
    return None


async def grade_schema_sufficiency(
    question: str,
    original_question: str | None = None,
    diagnostics: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if diagnostics is None:
        return await _grade_schema_sufficiency(question, original_question)
    with schema_diagnostics_scope(diagnostics):
        return await _grade_schema_sufficiency(question, original_question)


async def _grade_schema_sufficiency(
    question: str, original_question: str | None = None
) -> Dict[str, Any]:
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
    original_question = original_question or question
    attempts = 0
    last_missing: List[str] = []
    matched_rules = _matched_business_rules(original_question)
    if question != original_question:
        rewritten_rules = _matched_business_rules(question)
        if matched_rules is None:
            matched_rules = rewritten_rules
        elif rewritten_rules:
            rules_by_term = {
                rule.get("term"): rule for rule in matched_rules
                if isinstance(rule, dict)
            }
            rules_by_term.update({
                rule.get("term"): rule for rule in rewritten_rules
                if isinstance(rule, dict)
            })
            matched_rules = list(rules_by_term.values())
    available_calculated_metrics = _matched_calculated_metrics(matched_rules)
    active_offer_rules = [
        rule for rule in matched_rules or []
        if rule.get("term") == "active_offer_status"
    ]

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
        relevant_tables = await retrieve_relevant_tables(
            original_question, top_k=top_k
        )
        filtered_schema, relationships = _expand_schema_for_relationships(
            full_schema, relevant_tables, all_relationships, original_question
        )
        baseline_prompt_length = len(
            _build_schema_grader_prompt(
                original_question,
                question,
                _flatten_schema_for_prompt(filtered_schema),
                relationships,
                include_configured_search_guidance=False,
                available_calculated_metrics=available_calculated_metrics,
                matched_business_rules=matched_rules,
            )
        )
        filtered_schema, relationships = _add_configured_search_context(
            filtered_schema, full_schema, relationships, all_relationships
        )
        diagnostics = _ACTIVE_SCHEMA_DIAGNOSTICS.get()
        if diagnostics is not None:
            diagnostics["schema_attempts"].append({
                "attempt": attempts,
                "retrieved_tables": list(filtered_schema),
                "relationships": relationships,
            })

        schema_text = _flatten_schema_for_prompt(filtered_schema)
        prompt_length_after = len(
            _build_schema_grader_prompt(
                original_question,
                question,
                schema_text,
                relationships,
                matched_business_rules=matched_rules,
            )
        )
        logger.info(
            "Schema grader prompt characters before configured search context=%d "
            "after=%d",
            baseline_prompt_length,
            prompt_length_after,
        )
        if active_offer_rules and available_calculated_metrics:
            sufficient, missing = await _ask_cheap_model_if_sufficient(
                original_question,
                question,
                schema_text,
                relationships,
                available_calculated_metrics=available_calculated_metrics,
            )
        elif active_offer_rules:
            sufficient, missing = await _ask_cheap_model_if_sufficient(
                original_question,
                question,
                schema_text,
                relationships,
            )
        elif available_calculated_metrics:
            sufficient, missing = await _ask_cheap_model_if_sufficient(
                original_question,
                question,
                schema_text,
                relationships,
                available_calculated_metrics=available_calculated_metrics,
            )
        else:
            sufficient, missing = await _ask_cheap_model_if_sufficient(
                original_question, question, schema_text, relationships
            )
        normalized_missing_items = _normalize_missing_items(missing)
        irrelevant_view_items = [
            item for item in normalized_missing_items
            if _is_irrelevant_active_offer_view_item(item, active_offer_rules)
        ]
        if irrelevant_view_items:
            logger.info(
                "Ignoring schema grader requirements for editable_offers_view "
                "under the active-offer business rule: %s",
                [item["detail"] for item in irrelevant_view_items],
            )
            normalized_missing_items = [
                item for item in normalized_missing_items
                if item not in irrelevant_view_items
            ]
        missing_items = [
            item for item in normalized_missing_items
            if _metric_for_missing_column(item, available_calculated_metrics) is None
        ]
        sufficient = sufficient or not missing_items
        missing_details = [item["detail"] for item in missing_items]
        if diagnostics is not None:
            diagnostics["schema_attempts"][-1]["missing_items"] = missing_items
        logger.info(
            "Schema grader missing items on attempt %d: %s",
            attempts,
            missing_details,
        )

        if sufficient:
            logger.info("Schema graded sufficient on attempt %d (%d tables)", attempts, len(filtered_schema))
            return {
                "sufficient": True,
                "schema": filtered_schema,   # only the relevant tables go to Node 6 now
                "relationships": relationships,
                "attempts": attempts,
            }

        disconnected = _find_disconnected_pair(
            missing_items, full_schema, all_relationships
        )
        if disconnected is not None:
            pair, detail = disconnected
            if diagnostics is not None:
                diagnostics["schema_attempts"][-1]["missing_relationship_pair"] = {
                    "tables": list(pair),
                    "detail": detail,
                }
            logger.warning(
                "Schema grading found no verified relationship path between %s and %s (%s).",
                pair[0], pair[1], detail,
            )
            raise MissingRelationshipError(pair, detail)

        last_missing = missing_details
        logger.info("Widening schema retrieval after attempt %d.", attempts)

    # Exhausted retries without success -> Node 11 (human escalation) territory.
    diagnostics = _ACTIVE_SCHEMA_DIAGNOSTICS.get()
    if diagnostics is not None:
        diagnostics["schema_grader_error"] = (
            f"Schema not sufficient after {MAX_RETRIES} attempts."
        )
    raise GradeSampleError(
        f"Schema not sufficient for question after {MAX_RETRIES} attempts. "
        f"Last known missing items: {last_missing}"
    )