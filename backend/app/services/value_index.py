"""Small, database-backed English value catalog for query grounding."""

import hashlib
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List

import aiomysql
from decimal import Decimal

from app.db.mysql_client import MySQLNotConnectedError, fetch_all_readonly

logger = logging.getLogger(__name__)

VALUE_CATALOG_SQL = """
SELECT 'shop_details' AS source_table, 'name' AS source_column, id AS source_id, name AS value
FROM shop_details
WHERE status = 1 AND name IS NOT NULL AND TRIM(name) <> ''
UNION
SELECT 'subcategories', 'subcategoryname', id, subcategoryname
FROM subcategories
WHERE subcategoryname IS NOT NULL AND TRIM(subcategoryname) <> ''
UNION
SELECT 'categories', 'categoriesname', id, categoriesname
FROM categories
WHERE categoriesname IS NOT NULL AND TRIM(categoriesname) <> ''
UNION
SELECT 'shop_address', 'city', NULL, city
FROM shop_address
WHERE city IS NOT NULL AND TRIM(city) <> ''
UNION
SELECT 'offer', 'offer_heading', id, offer_heading
FROM offer
WHERE offer_heading IS NOT NULL AND TRIM(offer_heading) <> ''
"""

_WORD = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a", "an", "and", "any", "are", "at", "buy", "can",
    "best", "cheap", "contact", "find", "for", "from", "good", "highly",
    "i", "in", "is", "me", "near", "number", "of", "phone", "place",
    "places", "please", "pls", "rated", "rating", "ratings", "review",
    "reviews", "sell", "shop", "shops", "show", "the", "to", "top",
    "with",
}
_MAX_CANDIDATE_LENGTH = 160
_MIN_TOKEN_SIMILARITY = 0.76


class ValueIndexError(Exception):
    """Raised when the read-only value catalog cannot be built or used."""


_values: List[Dict[str, str]] = []
_version: str | None = None


def _tokens(value: str) -> List[str]:
    return [
        token
        for token in _WORD.findall(value.casefold())
        if token not in _STOP_WORDS and len(token) > 1
    ]


async def build_value_index() -> None:
    """Load distinct English search values from the read-only database pool."""
    global _values, _version

    try:
        rows = await fetch_all_readonly(VALUE_CATALOG_SQL)
    except (aiomysql.MySQLError, OSError, MySQLNotConnectedError) as exc:
        raise ValueIndexError(f"Could not load database value catalog: {exc}") from exc

    values: List[Dict[str, Any]] = []
    for row in rows:
        if len(row) != 4:
            raise ValueIndexError("Value catalog query returned an invalid row shape.")
        table, column, source_id, value = row
        if not all(isinstance(item, str) for item in (table, column, value)):
            continue
        if source_id is not None and (
            isinstance(source_id, bool) or not isinstance(source_id, (int, str, Decimal))
        ):
            raise ValueIndexError(f"Value catalog returned an invalid source identifier of type: {type(source_id)}")
        clean_value = value.strip()
        if clean_value and len(clean_value) <= _MAX_CANDIDATE_LENGTH:
            values.append({
                "source_table": table,
                "source_column": column,
                "source_id": source_id,
                "value": clean_value,
            })

    values.sort(
        key=lambda item: (
            item["source_table"],
            item["source_column"],
            item["source_id"] or 0,
            item["value"].casefold(),
        )
    )
    serialized = "\n".join(
        f"{item['source_table']}.{item['source_column']}="
        f"{item['source_id']}:{item['value']}"
        for item in values
    )
    _values = values
    _version = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    logger.info("Built English value index: %d distinct value(s)", len(values))


def get_value_index_version() -> str:
    if _version is None:
        raise ValueIndexError("Value index has not been built.")
    return _version


def resolve_value_candidates(
    question: str,
    *,
    limit_per_field: int = 5,
) -> List[Dict[str, Any]]:
    """Return ranked, typed candidates; never silently declares a match."""
    if _version is None:
        raise ValueIndexError("Value index has not been built.")

    question_tokens = _tokens(question)
    if not question_tokens:
        return []

    ranked: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for item in _values:
        value_tokens = _tokens(item["value"])
        token_scores = []
        matched_terms = []
        for question_token in question_tokens:
            best_score = max(
                (
                    1.0
                    if value_token == question_token
                    else SequenceMatcher(
                        None, value_token, question_token
                    ).ratio()
                )
                for value_token in value_tokens
            ) if value_tokens else 0.0
            if best_score >= _MIN_TOKEN_SIMILARITY:
                token_scores.append(best_score)
                matched_terms.append(question_token)

        if not token_scores:
            continue

        average_score = sum(token_scores) / len(token_scores)
        candidate = {
            **item,
            "matched_terms": matched_terms,
            "score": round(average_score, 3),
            "matched_token_count": len(matched_terms),
        }
        key = (item["source_table"], item["source_column"])
        ranked.setdefault(key, []).append(candidate)

    results: List[Dict[str, Any]] = []
    for key in sorted(ranked):
        candidates = ranked[key]
        candidates.sort(
            key=lambda item: (
                -item["matched_token_count"],
                -item["score"],
                item["value"].casefold(),
            )
        )
        if key == ("shop_details", "name") and candidates:
            best = candidates[0]
            tied_best = [
                candidate
                for candidate in candidates
                if candidate["matched_token_count"] == best["matched_token_count"]
                and candidate["score"] == best["score"]
            ]
            if (
                best["matched_token_count"] >= 2
                and len(tied_best) == 1
                and best.get("source_id") is not None
            ):
                best["unique_match"] = True
        results.extend(candidates[:limit_per_field])
    return results


def value_signature(candidates: List[Dict[str, Any]] | None) -> str:
    """Stable cache discriminator for the candidates used by query planning."""
    normalized = [
        (
            item.get("source_table"),
            item.get("source_column"),
            item.get("source_id"),
            item.get("value"),
            tuple(item.get("matched_terms", [])),
            item.get("unique_match", False),
        )
        for item in candidates or []
    ]
    return hashlib.sha256(repr(normalized).encode("utf-8")).hexdigest()


def derive_interpretation_notes(
    question: str,
    query: Dict[str, Any],
    candidates: List[Dict[str, Any]] | None,
) -> List[str]:
    """Create user-facing notes only from the validated plan and applied filters."""
    notes: List[str] = []
    query_tables = {query.get("table")}
    query_tables.update(
        join.get("table")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    )

    def leaves(expression: Any) -> List[Dict[str, Any]]:
        if not isinstance(expression, dict):
            return []
        children = expression.get("conditions")
        if isinstance(children, list):
            return [
                leaf
                for child in children
                for leaf in leaves(child)
            ]
        return [expression]

    predicates = leaves(query.get("where_expression"))
    applied_values = [
        (predicate.get("table"), predicate.get("column"), str(predicate.get("value", "")))
        for predicate in predicates
    ]
    for candidate in candidates or []:
        candidate_value = str(candidate.get("value", ""))
        for table, column, filter_value in applied_values:
            matched_filter = (
                table == candidate.get("source_table")
                and column == candidate.get("source_column")
                and (
                    (
                        candidate.get("source_table") == "shop_details"
                        and candidate.get("source_column") == "name"
                        and candidate.get("unique_match") is True
                        and str(candidate.get("source_id", ""))
                        == filter_value.strip("% ")
                    )
                    or (
                        candidate.get("source_id") is None
                        and filter_value.strip("% ").casefold()
                        == candidate_value.casefold()
                    )
                    or (
                        candidate.get("source_id") is not None
                        and candidate.get("unique_match") is not True
                        and filter_value.strip("% ").casefold()
                        == candidate_value.casefold()
                    )
                )
            )
            if matched_filter:
                note = (
                    f"Used the database value {candidate_value!r} for "
                    f"{table}.{column}."
                )
                if note not in notes:
                    notes.append(note)
                break

    question_lower = question.casefold()
    selected = query.get("select_items", [])
    orders = query.get("order_by", [])
    has_offer_min = any(
        isinstance(item, dict)
        and item.get("table") == "offer"
        and item.get("column") == "offer_price"
        and item.get("aggregate") == "MIN"
        for item in selected
    )
    if re.search(r"\bcheap\b", question_lower) and has_offer_min and any(
        isinstance(order, dict) and order.get("direction") == "ASC"
        for order in orders
    ):
        notes.append(
            "I interpreted 'cheap' as ranking by the lowest listed offer price; "
            "I did not apply a price cutoff."
        )

    has_average_rating = any(
        isinstance(item, dict)
        and item.get("table") == "reviews"
        and item.get("column") == "rating"
        and item.get("aggregate") == "AVG"
        for item in selected
    )
    if (
        any(term in question_lower for term in ("good review", "best review"))
        and has_average_rating
        and query.get("having_expression") is None
    ):
        notes.append(
            "I interpreted 'good reviews' as ranking by average rating; "
            "I did not apply a rating threshold."
        )

    if re.search(r"\bnear\b", question_lower) and "shop_address" in query_tables and any(
        predicate.get("table") == "shop_address"
        and predicate.get("column") in {"city", "arearoadname", "nearbylandmark"}
        for predicate in predicates
    ):
        notes.append(
            "I matched stored address text; this is not a geographic-distance calculation."
        )

    product_intent = re.search(r"\b(?:buy|sell|selling|purchase|product)\b", question_lower)
    if product_intent and any(
        (table, column) in {
            ("shop_details", "name"),
            ("offer", "offer_heading"),
        }
        for table, column, _ in applied_values
    ):
        notes.append(
            "This is a text match in shop or offer names; it does not verify that "
            "the shop sells the product."
        )
    return notes
