"""Audit metric-table paths to common dimensions in the live MySQL schema.

Run from ``backend`` with:
    python -m app.tests.audit_schema_traversals
"""

import asyncio
from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from app.agent.nodes.blocked_tables import (
    filter_blocked_relationships,
    filter_blocked_schema,
)
from app.agent.nodes.schema_grounding import (
    _validated_relationships,
    fetch_relationships_from_information_schema,
    fetch_schema_from_information_schema,
)
from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
)

METRIC_TABLES = (
    "views",
    "reviews",
    "offer",
    "bookings",
    "offer_items_view",
    "editable_offers_view",
)
DIMENSION_TABLES = ("categories", "subcategories", "users")


def _find_path(
    relationships: Iterable[Mapping[str, Any]],
    source: str,
    target: str,
) -> List[str] | None:
    graph: Dict[str, List[str]] = {}
    for relationship in relationships:
        table = relationship.get("table")
        parent = relationship.get("referenced_table")
        if isinstance(table, str) and isinstance(parent, str):
            graph.setdefault(table, []).append(parent)

    pending = deque([(source, [source])])
    visited = {source}
    while pending:
        table, path = pending.popleft()
        if table == target:
            return path
        for parent in graph.get(table, []):
            if parent not in visited:
                visited.add(parent)
                pending.append((parent, [*path, parent]))
    return None


def find_missing_traversals(
    tables: Mapping[str, Any],
    relationships: Sequence[Mapping[str, Any]],
    metric_tables: Sequence[str] = METRIC_TABLES,
    dimension_tables: Sequence[str] = DIMENSION_TABLES,
) -> List[Tuple[str, str]]:
    """Return metric/dimension pairs without a path in the published catalog."""
    available_metrics = [table for table in metric_tables if table in tables]
    available_dimensions = [table for table in dimension_tables if table in tables]
    return [
        (metric, dimension)
        for metric in available_metrics
        for dimension in available_dimensions
        if metric != dimension
        and _find_path(relationships, metric, dimension) is None
    ]


async def audit_live_schema() -> int:
    raw_tables = await fetch_schema_from_information_schema()
    if not raw_tables:
        raise RuntimeError("No tables were returned from the live database schema.")

    declared = await fetch_relationships_from_information_schema()
    validated = _validated_relationships(raw_tables, declared)
    published_tables = filter_blocked_schema(raw_tables)
    published_relationships = filter_blocked_relationships(validated)
    metric_tables = [
        table for table in METRIC_TABLES if table in published_tables
    ]
    dimension_tables = [
        table for table in DIMENSION_TABLES if table in published_tables
    ]

    print(
        f"Schema tables: {len(published_tables)} | "
        f"Declared FK edges: {len(declared)} | "
        f"Validated catalog edges: {len(published_relationships)}"
    )
    print(f"Metric tables audited: {', '.join(metric_tables) or '(none)'}")
    print(f"Dimensions audited: {', '.join(dimension_tables) or '(none)'}")

    for metric in metric_tables:
        for dimension in dimension_tables:
            if metric == dimension:
                continue
            path = _find_path(published_relationships, metric, dimension)
            if path:
                print(f"OK: {' -> '.join(path)}")

    missing = find_missing_traversals(
        published_tables,
        published_relationships,
        metric_tables=metric_tables,
        dimension_tables=dimension_tables,
    )
    if missing:
        print("MISSING TRAVERSAL PATHS:")
        for metric, dimension in missing:
            print(f"  {metric} -> {dimension}")
        return 1

    print("Audit passed: 0 metric-to-dimension traversal gaps.")
    return 0


async def main() -> int:
    await connect_to_mysql()
    try:
        return await audit_live_schema()
    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
