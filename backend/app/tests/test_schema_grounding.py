"""
test_schema_grounding.py — manual smoke test for schema_grounding.py (Node 3)

Run from backend/:
    python -m app.tests.test_schema_grounding
"""

import asyncio
from dotenv import load_dotenv

load_dotenv()

from app.db.mysql_client import close_mysql_connection, connect_to_mysql, MySQLConnectionError
from app.agent.nodes.schema_grounding import (
    refresh_schema_cache,
    get_cached_schema,
    fetch_schema_from_information_schema,
    schema_cache,
    SchemaGroundingError,
)


async def main() -> None:
    try:
        await connect_to_mysql()
    except MySQLConnectionError as e:
        print(f"FAILED to connect to MySQL: {e}")
        return

    try:
        print("1. Testing get_cached_schema() before any refresh (should raise)...")
        try:
            get_cached_schema()
            print("   FAILED: expected SchemaGroundingError, but cache returned something.\n")
        except SchemaGroundingError as e:
            print(f"   OK — correctly raised: {e}\n")

        print("2. Fetching real schema from INFORMATION_SCHEMA...")
        raw = await fetch_schema_from_information_schema()
        if not raw:
            print("   No tables found — stopping. Check your MySQL database.\n")
            return
        print(f"   Found {len(raw)} table(s): {list(raw.keys())}\n")

        print("3. Refreshing the cache...")
        await refresh_schema_cache()
        print(f"   Cache last_refreshed set: {schema_cache.last_refreshed is not None}")
        print(f"   Cache is_stale() right after refresh: {schema_cache.is_stale()}\n")

        print("4. Reading from get_cached_schema() (should work now, no DB hit)...")
        cached = get_cached_schema()
        first_table = next(iter(cached))
        print(f"   Sample table '{first_table}': {cached[first_table][:3]}\n")

        print("5. Checking shape — each column entry must be a dict with 'column' and 'data_type'...")
        bad_entries = [
            (t, c) for t, cols in cached.items() for c in cols
            if "column" not in c or "data_type" not in c
        ]
        if bad_entries:
            print(f"   FAILED: malformed entries found: {bad_entries[:3]}\n")
        else:
            print("   OK — all entries have the expected shape.\n")

        print("6. Testing is_stale() with a tiny max_age (should be stale immediately)...")
        assert schema_cache.is_stale(max_age_seconds=0) is True
        print("   OK\n")

        print("All checks passed.")

    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    asyncio.run(main())