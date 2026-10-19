"""
test_run_self_fix.py — manual smoke test for run_self_fix.py (Node 8, basic version)

Uses hand-built, already-safety-checked query dicts (skips Nodes 1-7) so
this test only exercises execution logic, not the whole pipeline.

Run from backend/:
    python -m app.tests.test_run_self_fix
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.db.mongo_client import close_mongo_connection, connect_to_mongo, MongoConnectionError
from app.agent.nodes.run_self_fix import run_query, QueryExecutionError

# These look like Node 7's output shape — collection/operation/filter or
# pipeline/limit/timeout_ms — since that's what this node expects as input.
TEST_QUERIES = [
    {
        "label": "Simple find on accounts",
        "query": {
            "collection": "accounts",
            "operation": "find",
            "filter": {"limit": {"$gte": 10000}},
            "pipeline": None,
            "limit": 5,
            "timeout_ms": 10000,
        },
    },
    {
        "label": "Aggregate: count buy transactions",
        "query": {
            "collection": "transactions",
            "operation": "aggregate",
            "filter": None,
            "pipeline": [
                {"$match": {"transactions.transaction_code": "buy"}},
                {"$count": "total_buy_documents"},
                {"$limit": 5},
            ],
            "limit": 5,
            "timeout_ms": 10000,
        },
    },
    {
        "label": "Find on a field that doesn't exist (should return 0 results, not error)",
        "query": {
            "collection": "accounts",
            "operation": "find",
            "filter": {"this_field_does_not_exist": "anything"},
            "pipeline": None,
            "limit": 5,
            "timeout_ms": 10000,
        },
    },
]


async def main() -> None:
    try:
        await connect_to_mongo()
    except MongoConnectionError as e:
        print(f"FAILED to connect to MongoDB: {e}")
        return

    try:
        for case in TEST_QUERIES:
            print(f"Case: {case['label']}")
            try:
                result = await run_query(case["query"])
                print(f"   Count: {result['count']}")
                if result["results"]:
                    print(f"   First result: {result['results'][0]}")
                print()
            except QueryExecutionError as e:
                print(f"   FAILED: {e}\n")

        print("All checks completed.")

    finally:
        await close_mongo_connection()


if __name__ == "__main__":
    asyncio.run(main())