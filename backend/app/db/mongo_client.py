"""
mongo_client.py

Manages the async MongoDB connection for the app.

Why Motor (not pymongo directly):
Motor is the async driver for MongoDB. FastAPI is async, so if we used the
regular blocking pymongo driver, every DB call would freeze the whole server
for other users until it finished. Motor lets other requests keep being
handled while one query is running.

Read-only enforcement (3 layers, per project design principle
"Never trust AI output blindly"):
  1. Connection level: we recommend a MongoDB user that only has 'read' role
     (set up on the MongoDB Atlas / server side, not in this file).
  2. App level: `assert_read_only_operation()` below blocks write-type
     operations before they ever reach the database.
  3. Query level (later, Node 7 - Safety Check): the generated query itself
     is inspected before execution.
This file handles layer 1 (connection) and provides the layer 2 guard.
"""

import logging
import os
from typing import Optional
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import ConnectionFailure, ConfigurationError, ServerSelectionTimeoutError
load_dotenv()
logger = logging.getLogger(__name__)

# Operations that mutate data. If a generated query tries to call any of
# these, we block it immediately rather than letting it reach MongoDB.
DISALLOWED_OPERATIONS = {
    "insert_one", "insert_many",
    "update_one", "update_many",
    "delete_one", "delete_many",
    "replace_one",
    "drop", "drop_database", "drop_collection",
    "rename", "create_index", "drop_index",
    "bulk_write", "find_one_and_delete",
    "find_one_and_replace", "find_one_and_update",
}


class MongoConnectionError(Exception):
    """Raised when the app cannot establish or verify a MongoDB connection."""
    pass


class MongoNotConnectedError(Exception):
    """Raised when code tries to use the database before startup connected it."""
    pass


class MongoDB:
    """
    Holds a single shared Motor client for the app's lifetime.

    We keep exactly one client instance (not one per request) because Motor's
    client already manages an internal connection pool efficiently — creating
    a new client per request would exhaust connections under real traffic.
    """

    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None


mongodb = MongoDB()


async def connect_to_mongo() -> None:
    """
    Opens the MongoDB connection. Call this once, on FastAPI startup.

    Raises MongoConnectionError if the URI is missing or the server is
    unreachable — we fail loudly at startup rather than discovering a broken
    DB connection later when a real user sends a query.
    """
    mongo_uri = os.getenv("MONGO_URI")
    db_name = os.getenv("MONGO_DB_NAME")

    if not mongo_uri:
        raise MongoConnectionError(
            "MONGO_URI is not set. Add it to your .env file."
        )
    if not db_name:
        raise MongoConnectionError(
            "MONGO_DB_NAME is not set. Add it to your .env file."
        )

    try:
        # serverSelectionTimeoutMS: how long to wait before giving up on
        # finding a usable server. Default is 30s, which is too long to make
        # a user wait during startup — we fail fast at 5s instead.
        mongodb.client = AsyncIOMotorClient(
            mongo_uri,
            serverSelectionTimeoutMS=5000,
            maxPoolSize=50,       # caps concurrent connections; protects the
                                   # DB from being overwhelmed under load
            minPoolSize=5,        # keeps a few connections warm so the first
                                   # requests after startup aren't slow
            retryWrites=False,    # explicit: this app should never write
        )

        # The client above doesn't actually connect yet (Motor connects
        # lazily). We force a real round-trip here with 'ping' so startup
        # fails immediately if MongoDB is unreachable, instead of failing
        # silently on the first real user request.
        await mongodb.client.admin.command("ping")

        mongodb.db = mongodb.client[db_name]
        logger.info("Connected to MongoDB database '%s'", db_name)

    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        raise MongoConnectionError(
            f"Could not reach MongoDB server: {e}"
        ) from e
    except ConfigurationError as e:
        raise MongoConnectionError(
            f"Invalid MONGO_URI configuration: {e}"
        ) from e


async def close_mongo_connection() -> None:
    """Closes the MongoDB connection. Call this on FastAPI shutdown."""
    if mongodb.client is not None:
        mongodb.client.close()
        logger.info("MongoDB connection closed")


def get_database() -> AsyncIOMotorDatabase:
    """
    Returns the active database handle for use in nodes/services.

    Raises MongoNotConnectedError instead of returning None if called before
    connect_to_mongo() has run — this turns a silent bug (code trying to
    query a None object) into a clear, immediate error.
    """
    if mongodb.db is None:
        raise MongoNotConnectedError(
            "Database is not connected. Was connect_to_mongo() called on startup?"
        )
    return mongodb.db


def assert_read_only_operation(operation_name: str) -> None:
    """
    Guard function: raises PermissionError if a write-type operation name
    is passed in. Every node that runs a dynamically generated query should
    call this first, before executing it.

    This is layer 2 of the read-only enforcement described at the top of
    this file — an app-level check that runs even before Node 7's dedicated
    safety check, as defense in depth (multiple independent safeguards, so
    one bug or bypass doesn't mean unrestricted access).
    """
    if operation_name in DISALLOWED_OPERATIONS:
        raise PermissionError(
            f"Operation '{operation_name}' is not allowed. "
            f"This system is read-only."
        )


async def health_check() -> bool:
    """
    Returns True if MongoDB is reachable right now, False otherwise.
    Used by the /health endpoint so monitoring tools can detect DB outages.
    """
    if mongodb.client is None:
        return False
    try:
        await mongodb.client.admin.command("ping")
        return True
    except (ConnectionFailure, ServerSelectionTimeoutError):
        return False