"""Tables the agent must never read, join, or show to any model."""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Mapping

logger = logging.getLogger(__name__)

# table name -> reason (kept for reviewers and logs)
BLOCKED_TABLES: Dict[str, str] = {
    "products": (
        "offer.product_id points to missing rows for some offers; "
        "use offer.offer_heading as the item name instead"
    ),
    "otps": "holds one-time passwords",
    "sessions": "holds login session payloads and IP addresses",
    "password_reset_tokens": "holds password reset tokens and emails",
}


def blocked_table_names() -> frozenset:
    return frozenset(name.lower() for name in BLOCKED_TABLES)


def filter_blocked_schema(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the schema without blocked tables."""
    blocked = blocked_table_names()
    return {
        table: columns
        for table, columns in schema.items()
        if table.lower() not in blocked
    }


def filter_blocked_relationships(
    relationships: Iterable[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Drop any relationship that touches a blocked table."""
    blocked = blocked_table_names()
    return [
        rel
        for rel in relationships
        if str(rel.get("table", "")).lower() not in blocked
        and str(rel.get("referenced_table", "")).lower() not in blocked
    ]


def find_blocked_tables(table_names: Iterable[str]) -> List[str]:
    """Return the blocked tables found in a list of table names."""
    blocked = blocked_table_names()
    return sorted({
        name for name in table_names if name and name.lower() in blocked
    })


def validate_blocked_tables(raw_schema: Mapping[str, Any]) -> None:
    """Startup check. Call it with the UNFILTERED schema."""
    from app.agent.search_config import SEARCHABLE_COLUMNS

    search_tables = {
        field["table"].lower()
        for fields in SEARCHABLE_COLUMNS.values()
        for field in fields
    }
    overlap = sorted(blocked_table_names() & search_tables)
    if overlap:
        raise ValueError(
            f"Blocked tables cannot also be search tables: {overlap}"
        )

    existing = {table.lower() for table in raw_schema}
    for table in BLOCKED_TABLES:
        if table.lower() not in existing:
            # A typo here would leave the block silently ineffective.
            logger.warning(
                "Blocked table '%s' is not in the database schema. "
                "Check the spelling in BLOCKED_TABLES.",
                table,
            )