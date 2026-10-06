"""
test_generate_query.py — manual smoke test for generate_query.py (Node 6)

Calls the real Groq API (strong model) — costs more tokens than the other
tests since this uses the bigger model. Uses REAL cached schema from Node 3
and business rules from Node 5 as input, so a failure here could mean a bug
in this node, OR bad input from an earlier node — check test_schema_grounding.py
and test_business_rules.py pass first if this one fails.

Run from backend/:
    python -m app.tests.test_generate_query
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.db.mysql_client import close_mysql_connection, connect_to_mysql, MySQLConnectionError
from app.agent.nodes.schema_grounding import refresh_schema_cache, get_cached_schema, SchemaGroundingError
from app.agent.nodes.business_rules import find_relevant_rules
from app.agent.nodes.generate_query import (
    generate_query,
    QueryGenerationError,
    UnanswerableQuestionError,
)

# (question, should_be_answerable)
TEST_QUESTIONS = [
    ("Show me all the users in the database", True),
    ("List all advertisements with a radius greater than 10", True),
    ("Find the names of all the plans we offer", True),
    ("What is the meaning of life", False),  # should come back unanswerable, not a crash
]


async def main() -> None:
    try:
        await connect_to_mysql()
    except MySQLConnectionError as e:
        print(f"FAILED to connect to MySQL: {e}")
        return

    try:
        print("Loading real schema from cache (Node 3)...")
        try:
            await refresh_schema_cache()
            full_schema = get_cached_schema()
        except SchemaGroundingError as e:
            print(f"FAILED to load schema: {e}")
            return

        # Filter the schema to only include a few core tables for the test
        core_tables = ["users", "plans", "subscriptions", "advertisements", "invoices"]
        schema_context = {k: v for k, v in full_schema.items() if k in core_tables}

        print(f"   Tables available: {list(schema_context.keys())}\n")

        for question, should_be_answerable in TEST_QUESTIONS:
            print(f"Question: {question!r}")
            matched_rules = find_relevant_rules(question)
            print(f"   Matched business rules: {[r['term'] for r in matched_rules]}")

            try:
                result = await generate_query(question, schema_context, matched_rules)
                if not should_be_answerable:
                    print("   UNEXPECTED: got a query back for a question we expected to be unanswerable.")

                print(f"   Table:            {result.get('table')}")
                print(f"   Select Items:     {result.get('select_items')}")
                print(f"   Where Conditions: {result.get('where_conditions')}")
                if result.get("group_by"):
                    print(f"   Group By:         {result.get('group_by')}")
                print(f"   Reasoning:        {result.get('reasoning')}\n")

            except UnanswerableQuestionError as e:
                if should_be_answerable:
                    print(f"   UNEXPECTED: marked unanswerable when we expected a query. Reasoning: {e.reasoning}\n")
                else:
                    print(f"   Correctly marked unanswerable: {e.reasoning}\n")

            except QueryGenerationError as e:
                print(f"   FAILED: {e}\n")

        print("All checks completed.")

    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    asyncio.run(main())