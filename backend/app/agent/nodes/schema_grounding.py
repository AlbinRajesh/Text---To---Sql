"""
schema_grounding.py  (Node 3: Schema Grounding — cached, MySQL version)

Fetches real schema metadata (tables, columns, types) from INFORMATION_SCHEMA
ONCE, caches it in memory, and refreshes it on a schedule or manual trigger.
This replaces sag_sampling.py's live-row-sampling as the primary grounding
source — schema structure barely changes, so we stop paying the round-trip
cost on every single user question.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db.mysql_client import fetch_all
from app.agent.relationship_overrides import CURATED_RELATIONSHIP_OVERRIDES
from app.agent.search_config import SearchConfigurationError, validate_searchable_columns
from app.agent.nodes.blocked_tables import (
    filter_blocked_relationships,
    filter_blocked_schema,
    validate_blocked_tables,
)

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # daily


class SchemaGroundingError(Exception):
    """Raised when the schema cache cannot be built or refreshed."""
    pass


class SchemaCache:
    """Holds the in-memory schema snapshot and its freshness state."""

    def __init__(self) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.relationships: List[Dict[str, Any]] = []
        self.ambiguous_names: Dict[str, List[str]] = {}
        self.last_refreshed: Optional[float] = None

    def is_stale(self, max_age_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS) -> bool:
        if self.last_refreshed is None:
            return True
        return (time.time() - self.last_refreshed) > max_age_seconds

    def as_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        return self.tables


schema_cache = SchemaCache()


async def fetch_schema_from_information_schema() -> Dict[str, List[Dict[str, Any]]]:
    """
    Queries INFORMATION_SCHEMA once for all tables/columns/types in the
    current database. Runs on the PRIMARY connection (fetch_all), not the
    read replica — this is metadata, not user query execution.
    """
    try:
        rows = await fetch_all(
            """
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """
        )
    except Exception as e:
        logger.error("Failed to fetch schema from INFORMATION_SCHEMA: %s", e)
        raise SchemaGroundingError(f"Could not fetch schema: {e}") from e

    result: Dict[str, List[Dict[str, Any]]] = {}
    for table_name, column_name, data_type, is_nullable, column_key in rows:
        result.setdefault(table_name, []).append({
            "column": column_name,
            "data_type": data_type,
            "nullable": is_nullable == "YES",
            "key": column_key,
        })

    if not result:
        raise SchemaGroundingError("No tables found in database. Cache would be empty.")

    return result


async def fetch_relationships_from_information_schema() -> List[Dict[str, Any]]:
    """Fetch declared foreign keys, preserving all column pairs in composite keys."""
    try:
        rows = await fetch_all(
            """
            SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME,
                   REFERENCED_COLUMN_NAME, CONSTRAINT_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND REFERENCED_TABLE_SCHEMA = DATABASE()
              AND REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
            """
        )
    except Exception as e:
        logger.error("Failed to fetch foreign-key relationships: %s", e)
        raise SchemaGroundingError(f"Could not fetch foreign-key relationships: {e}") from e

    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for table, column, referenced_table, referenced_column, constraint in rows:
        key = (table, constraint, referenced_table)
        relationship = grouped.setdefault(key, {
            "constraint_name": constraint,
            "table": table,
            "referenced_table": referenced_table,
            "column_pairs": [],
        })
        relationship["column_pairs"].append({
            "column": column,
            "referenced_column": referenced_column,
        })
    return list(grouped.values())


def _validated_relationships(
    tables: Dict[str, List[Dict[str, Any]]],
    declared: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Combine declared and explicitly curated edges; never infer joins by name."""
    relationships: List[Dict[str, Any]] = []
    seen = set()
    valid_columns = {
        name: {column["column"] for column in columns if isinstance(column, dict) and "column" in column}
        for name, columns in tables.items()
    }
    for relationship in [*declared, *CURATED_RELATIONSHIP_OVERRIDES]:
        if not isinstance(relationship, dict):
            raise SchemaGroundingError("Relationship definition must be an object.")
        table = relationship.get("table")
        referenced_table = relationship.get("referenced_table")
        constraint_name = relationship.get("constraint_name")
        pairs = relationship.get("column_pairs")
        cardinality = relationship.get("cardinality", "many_to_one")
        verification = relationship.get(
            "verification",
            "declared_foreign_key" if relationship in declared else None,
        )
        if (
            not isinstance(table, str)
            or table not in tables
            or not isinstance(referenced_table, str)
            or referenced_table not in tables
            or table == referenced_table
            or not isinstance(constraint_name, str)
            or not constraint_name
            or cardinality not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
            or verification not in {"declared_foreign_key", "reviewed_manual_query"}
            or not isinstance(pairs, list)
            or not pairs
        ):
            raise SchemaGroundingError(
                f"Invalid relationship definition for {table!r} -> {referenced_table!r}."
            )

        normalized_pairs = []
        for pair in pairs:
            if not isinstance(pair, dict):
                raise SchemaGroundingError("Relationship column pair must be an object.")
            column = pair.get("column")
            referenced_column = pair.get("referenced_column")
            if (
                not isinstance(column, str)
                or column not in valid_columns[table]
                or not isinstance(referenced_column, str)
                or referenced_column not in valid_columns[referenced_table]
            ):
                raise SchemaGroundingError(
                    "Relationship references an unknown column: "
                    f"{table}.{column} -> {referenced_table}.{referenced_column}."
                )
            normalized_pairs.append({
                "column": column,
                "referenced_column": referenced_column,
            })
        if len({(p["column"], p["referenced_column"]) for p in normalized_pairs}) != len(normalized_pairs):
            raise SchemaGroundingError("Relationship contains duplicate column pairs.")


        single_row_when = relationship.get("single_row_when")
        if single_row_when is not None:
            if (
                not isinstance(single_row_when, dict)
                or set(single_row_when) != {"column", "value"}
                or single_row_when["column"] not in valid_columns[table]
                or isinstance(single_row_when["value"], bool)
                or not isinstance(single_row_when["value"], (int, str))
            ):
                raise SchemaGroundingError(
                    f"Invalid single_row_when for {table!r} -> {referenced_table!r}."
                )
            
        identity = (
            table,
            referenced_table,
            tuple(sorted((p["column"], p["referenced_column"]) for p in normalized_pairs)),
        )
        if identity in seen:
            continue
        seen.add(identity)
        entry = {
            "constraint_name": constraint_name,
            "table": table,
            "referenced_table": referenced_table,
            "cardinality": cardinality,
            "verification": verification,
            "column_pairs": normalized_pairs,
        }
        if single_row_when is not None:
            entry["single_row_when"] = dict(single_row_when)
        relationships.append(entry)
    return relationships

# (table, name column) pairs where a repeated name makes a name filter ambiguous.
_NAME_COLUMNS = (
    ("subcategories", "subcategoryname"),
    ("categories", "categoriesname"),
)


def _first_value(row: Any) -> Any:
    """Rows may be tuples or dicts depending on the cursor type."""
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


async def fetch_ambiguous_names(
    tables: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[str]]:
    """Names that appear more than once, keyed as 'table.column'.
    Table/column names come from the constant above, never from user input."""
    result: Dict[str, List[str]] = {}
    for table, column in _NAME_COLUMNS:
        known_columns = {entry.get("column") for entry in tables.get(table, [])}
        if column not in known_columns:
            continue
        rows = await fetch_all(
            f"SELECT `{column}` FROM `{table}` "
            f"WHERE `{column}` IS NOT NULL "
            f"GROUP BY `{column}` HAVING COUNT(*) > 1",
            (),
        )
        result[f"{table}.{column}"] = sorted(
            {str(_first_value(row)).strip().casefold() for row in rows}
        )
    return result

async def refresh_schema_cache() -> None:
    """
    Rebuilds the in-memory schema cache. Call this from FastAPI's lifespan
    on startup, and optionally from a scheduled background task or an
    admin-triggered endpoint.
    """
    raw_schema = await fetch_schema_from_information_schema()
    raw_relationships = await fetch_relationships_from_information_schema()
    validate_blocked_tables(raw_schema)
    tables = filter_blocked_schema(raw_schema)
    declared_relationships = filter_blocked_relationships(raw_relationships)
    relationships = filter_blocked_relationships(
        _validated_relationships(tables, declared_relationships)
    )
    try:
        validate_searchable_columns(tables, relationships)
    except SearchConfigurationError as exc:
        raise SchemaGroundingError(
            f"Invalid searchable-columns configuration: {exc}"
        ) from exc
    schema_cache.tables = tables
    schema_cache.ambiguous_names = await fetch_ambiguous_names(tables)
    schema_cache.relationships = relationships
    schema_cache.last_refreshed = time.time()
    logger.info(
        "Schema cache refreshed: %d table(s), %d verified relationship(s) loaded",
        len(schema_cache.tables), len(schema_cache.relationships),
    )


def get_cached_schema() -> Dict[str, List[Dict[str, Any]]]:
    """
    Used by Node 6 (generate_query) and Node 4 (grade_sample) instead of
    hitting the database. Raises if the cache was never built.
    """
    if not schema_cache.tables:
        raise SchemaGroundingError(
            "Schema cache is empty. Was refresh_schema_cache() called on startup?"
        )
    return schema_cache.as_dict()


def get_cached_relationships() -> List[Dict[str, Any]]:
    """Return only declared or explicitly curated, schema-validated relationships."""
    if not schema_cache.tables:
        raise SchemaGroundingError(
            "Schema cache is empty. Was refresh_schema_cache() called on startup?"
        )
    return list(schema_cache.relationships)

def get_ambiguous_names() -> Dict[str, List[str]]:
    """Repeated category/subcategory names. Empty if the cache is not built."""
    return dict(schema_cache.ambiguous_names)