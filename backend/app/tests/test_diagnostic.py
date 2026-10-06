# diagnostic.py
import asyncio
from dotenv import load_dotenv
load_dotenv()

from app.db.mysql_client import connect_to_mysql, close_mysql_connection
from app.agent.nodes.schema_grounding import refresh_schema_cache, get_cached_schema
from app.services.embedding_service import build_schema_index, _index, _embed_texts
import numpy as np

async def main():
    await connect_to_mysql()
    await refresh_schema_cache()
    await build_schema_index(get_cached_schema())

    questions = ["What categories exist", "Show me OTP records", "List all products for sale"]
    for q in questions:
        qvec = (await _embed_texts([q]))[0]
        sims = _index.embeddings @ qvec
        idx = _index.table_names.index("active_logins")
        rank = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True).index(idx) + 1
        print(f"{q!r}: active_logins similarity={sims[idx]:.4f}, rank={rank}/{len(sims)}")

    await close_mysql_connection()

asyncio.run(main())