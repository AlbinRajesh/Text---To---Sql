"""
test_generate_query_isolated.py — quick single-question test for Node 6.
Run from backend/:
    python -m app.tests.test_generate_query_isolated
"""

import asyncio
from dotenv import load_dotenv
load_dotenv()

from app.agent.nodes.schema_grounding import refresh_schema_cache, get_cached_schema, SchemaGroundingError
from app.services.embedding_service import build_schema_index, retrieve_relevant_tables, filter_schema_to_tables
from app.agent.nodes.generate_query import generate_query, QueryGenerationError, UnanswerableQuestionError
from app.db.mysql_client import connect_to_mysql, connect_to_read_replica, close_mysql_connection

QUESTION = "How many plans do we have in total"
RUNS = 5  # run several times to check the retry logic across multiple attempts
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

async def main():
    await connect_to_mysql()
    await connect_to_read_replica()
    try:
        await refresh_schema_cache()
        await build_schema_index(get_cached_schema())  # build the index ONCE

        full_schema = get_cached_schema()
        relevant_tables = await retrieve_relevant_tables(QUESTION, top_k=6)
        schema_context = filter_schema_to_tables(full_schema, relevant_tables)

        for i in range(1, RUNS + 1):
            print(f"\n--- Run {i}/{RUNS} ---")
            try:
                result = await generate_query(QUESTION, schema_context, matched_business_rules=[])
                print("select_items:", result["select_items"])
                print("reasoning:", result["reasoning"])
            except UnanswerableQuestionError as e:
                print("Marked unanswerable:", e.reasoning)
            except QueryGenerationError as e:
                print("QueryGenerationError:", e)
    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    asyncio.run(main())