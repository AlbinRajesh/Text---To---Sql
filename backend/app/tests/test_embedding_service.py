"""
test_embedding_service.py — manual smoke test for embedding_service.py

Tests index building, exact-name guarantee, semantic-only matching (no
shared words), and the "nothing truly relevant" edge case. Uses the local FastEmbed model for embeddings.

Run from backend/:
    python -m app.tests.test_embedding_service
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.db.mysql_client import close_mysql_connection, connect_to_mysql, MySQLConnectionError
from app.agent.nodes.schema_grounding import refresh_schema_cache, get_cached_schema, SchemaGroundingError
from app.services.embedding_service import (
    build_schema_index,
    retrieve_relevant_tables,
    filter_schema_to_tables,
    EmbeddingServiceError,
)

# (question, expect_this_table_in_results)
TEST_CASES = [
    ("Show me all invoices", "invoices"),
    ("Who is paying us money", "invoices"),
    ("List every user account", "users"),
    ("What plans do we offer customers", "plans"),
    ("Show me all advertisements with radius over 10", "advertisements"),

    # --- New edge cases ---
    ("Show me subscription details", "subscriptions"),           # plural mismatch, like users
    ("What coupons have been used", "coupon_usages"),             # plural + compound name
    ("Show me all shops", "shop_details"),                        # synonym, not exact word
    ("List all products for sale", "products"),                   # exact
    ("Show me customer reviews", "reviews"),                      # exact
    ("What login attempts failed", "login_logs"),                 # semantic, compound name
    ("Show me OTP records", "otps"),                              # abbreviation, exact-ish
    ("List all shop addresses", "shop_address"),                  # exact but singular/compound
    ("Show me active subscriptions only", "subscriptions"),       # business term "active" + plural
    ("What categories exist", "categories"),                      # exact
    ("Show me favourited items", "favourite"),                    # word-form mismatch (favourited vs favourite)
    ("List all bookings made this month", "bookings"),            # exact + extra words
    ("Show me users who liked something", "likes"),               # multi-table question, ambiguous on purpose
    ("What subcategories are under categories", "subcategories"), # exact but could confuse with 'categories'
]
NONSENSE_QUESTION = "What is the meaning of life"


async def main() -> None:
    try:
        await connect_to_mysql()
    except MySQLConnectionError as e:
        print(f"FAILED to connect to MySQL: {e}")
        return

    try:
        print("Loading schema and building index...")
        try:
            await refresh_schema_cache()
            schema = get_cached_schema()
            await build_schema_index(schema)
        except (SchemaGroundingError, EmbeddingServiceError) as e:
            print(f"FAILED to build index: {e}")
            return
        print(f"   Index built with {len(schema)} table(s).\n")

        print("1. Testing retrieval accuracy per question...")
        for question, expected_table in TEST_CASES:
            tables = await retrieve_relevant_tables(question, top_k=6)
            hit = expected_table in tables
            status = "OK" if hit else "MISS"
            await asyncio.sleep(1.5)
            print(f"   [{status}] {question!r}")
            print(f"        expected: '{expected_table}' | got: {tables}")
        print()

        print("2. Testing exact-name guarantee (table name literally in question)...")
        tables = await retrieve_relevant_tables("show me the users table", top_k=6)
        assert "users" in tables, "Exact table name mention was not guaranteed-included!"
        print(f"   OK — 'users' guaranteed present: {tables}\n")

        print("3. Testing nonsense/unrelated question (should still return 6, just weak matches)...")
        tables = await retrieve_relevant_tables(NONSENSE_QUESTION, top_k=6)
        print(f"   Returned {len(tables)} table(s) even though none are truly relevant: {tables}")
        assert len(tables) == 6, f"Expected 6 tables even on a bad match, got {len(tables)}"
        print("   OK — retrieval never returns empty; downstream grading (Node 4) is what catches this.\n")

        print("4. Testing filter_schema_to_tables() slices correctly...")
        filtered = filter_schema_to_tables(schema, ["users", "invoices"])
        assert set(filtered.keys()) == {"users", "invoices"}, f"Filter returned wrong tables: {filtered.keys()}"
        print(f"   OK — filtered schema has exactly: {list(filtered.keys())}\n")

        print("5. Testing top_k widening (simulates grade_sample.py's retry behavior)...")
        for attempt in (1, 2, 3):
            top_k = 6 * attempt
            tables = await retrieve_relevant_tables("show me plans", top_k=top_k)
            print(f"   attempt {attempt} (top_k={top_k}): got {len(tables)} table(s)")
            assert len(tables) == min(top_k, len(schema)), "top_k widening did not return expected count"
        print("   OK\n")

        print("All checks completed.")

    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    asyncio.run(main())