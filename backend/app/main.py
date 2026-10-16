from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
logging.basicConfig(level=logging.INFO)
from app.db.mysql_client import (
    MySQLConnectionError,
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
    fetch_all,
    health_check,
)
from app.agent.nodes.schema_grounding import refresh_schema_cache, SchemaGroundingError
from app.services.embedding_service import build_schema_index
from app.agent.nodes.schema_grounding import (
    refresh_schema_cache,
    get_cached_schema,
    SchemaGroundingError,
)
from app.services.embedding_service import build_schema_index, EmbeddingServiceError
from app.api.chat import router as chat_router
from app.agent.nodes.business_rules import BusinessRulesError, get_business_rules_store
from app.services.suggestion_store import (
    close_suggestion_store,
    initialize_suggestion_store,
    suggestion_fast_path_available,
)
from app.services.pending_interactions import (
    close_pending_interaction_store,
    initialize_pending_interaction_store,
)
from app.services.value_index import ValueIndexError, build_value_index



@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await connect_to_mysql()
        await connect_to_read_replica()
        await refresh_schema_cache()
        await build_schema_index(get_cached_schema())
        await build_value_index()
        get_business_rules_store().version
        await initialize_suggestion_store()
        await initialize_pending_interaction_store()
    except (
        MySQLConnectionError,
        SchemaGroundingError,
        EmbeddingServiceError,
        BusinessRulesError,
        ValueIndexError,
    ) as e:
        raise RuntimeError(f"Startup failed: {e}") from e

    yield
    await close_pending_interaction_store()
    await close_suggestion_store()
    await close_mysql_connection()


app = FastAPI(title="SQL AI Query Agent", lifespan=lifespan)

# Allow React frontend to communicate with backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(chat_router)


@app.get("/")
def read_root():
    return {"message": "Hello from FastAPI!"}


@app.get("/api/status")
def get_status():
    return {"status": "Backend is running smoothly"}


@app.get("/api/health")
async def check_health():
    is_db_healthy = await health_check()
    return {
        "backend": "healthy",
        "database": "connected" if is_db_healthy else "disconnected",
        "suggestion_fast_path": (
            "available"
            if await suggestion_fast_path_available()
            else "unavailable"
        ),
    }


@app.get("/api/test-db")
async def test_db():
    tables = await fetch_all(
        """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
        """
    )
    return {
        "database": "connected",
        "tables": [row[0] for row in tables],
    }