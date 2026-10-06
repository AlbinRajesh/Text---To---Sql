"""
test_grade_sample.py — manual smoke test for grade_sample.py (Node 4)

Calls the real Groq API (cheap/fast model) and MySQL. Tests both the
"sufficient on first try" path and forces a look at the retry/refresh
path. No fake data — uses your real schema cache.

Run from backend/:
    python -m app.tests.test_grade_sample
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.db.mysql_client import close_mysql_connection, connect_to_mysql, MySQLConnectionError
from app.agent.nodes.schema_grounding import refresh_schema_cache, SchemaGroundingError
from app.agent.nodes.grade_sample import grade_schema_sufficiency, GradeSampleError

# (question, should_be_sufficient)
# Note: "should_be_sufficient" is our expectation, not a hard guarantee —
# the cheap model's judgment can vary. Read the reasoning if one flips.
TEST_QUESTIONS = [
    ("Show me all users in the database", True),
    ("List all advertisements with a radius greater than 10", True),
    ("What is the current price of Bitcoin", False),  # nothing in schema could answer this
]


async def main() -> None:
    try:
        await connect_to_mysql()
    except MySQLConnectionError as e:
        print(f"FAILED to connect to MySQL: {e}")
        return

    try:
        print("Pre-loading schema cache so Node 4 has something to grade...")
        try:
            await refresh_schema_cache()
        except SchemaGroundingError as e:
            print(f"FAILED to load schema: {e}")
            return
        print("   Cache loaded.\n")

        for question, should_be_sufficient in TEST_QUESTIONS:
            print(f"Question: {question!r}")
            try:
                result = await grade_schema_sufficiency(question)
                print(f"   sufficient: {result['sufficient']}")
                print(f"   attempts:   {result['attempts']}")
                print(f"   tables in returned schema: {list(result['schema'].keys())[:5]}...")

                if should_be_sufficient and not result["sufficient"]:
                    print("   NOTE: expected sufficient=True but got False — check model reasoning.\n")
                elif not should_be_sufficient and result["sufficient"]:
                    print("   NOTE: expected sufficient=False but got True — model may be too lenient.\n")
                else:
                    print("   OK — matched expectation.\n")

            except GradeSampleError as e:
                if should_be_sufficient:
                    print(f"   FAILED: exhausted retries when we expected success: {e}\n")
                else:
                    print(f"   OK — correctly exhausted retries and would escalate to Node 11: {e}\n")

        print("Testing behavior when cache is forced empty (should self-heal via refresh)...")
        from app.agent.nodes.schema_grounding import schema_cache
        schema_cache.tables = {}  # simulate an empty/never-built cache
        schema_cache.last_refreshed = None

        try:
            result = await grade_schema_sufficiency("Show me all users")
            print(f"   OK — self-healed. sufficient={result['sufficient']}, attempts={result['attempts']}\n")
        except GradeSampleError as e:
            print(f"   FAILED: could not self-heal from empty cache: {e}\n")

        print("All checks completed.")

    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    asyncio.run(main())