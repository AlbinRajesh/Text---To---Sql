"""Async MySQL connection management for the FastAPI application."""

import logging
import os
import time
from typing import Any, Optional

import aiomysql
from dotenv import load_dotenv

from app.services.timing import record_database_call

load_dotenv()
logger = logging.getLogger(__name__)


class MySQLConnectionError(Exception):
    """Raised when the application cannot establish a MySQL connection."""


class MySQLNotConnectedError(Exception):
    """Raised when the database is used before application startup connects."""


class MySQL:
    """Owns the application's shared async MySQL connection pools."""

    pool: Optional[aiomysql.Pool] = None          # primary (kept for admin/health only)
    read_pool: Optional[aiomysql.Pool] = None      # NEW: read-replica pool, used by all query nodes


mysql = MySQL()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise MySQLConnectionError(f"{name} is not set. Add it to the .env file.")
    return value


async def _init_session(connection: aiomysql.Connection) -> None:
    """
    Runs on every new connection in the read-replica pool.
    Enforces a hard session-level query timeout — this is a real DB-side
    limit, not just the SQL comment hint used before, so a stuck AI-generated
    query gets killed by MySQL itself even if the app-level timeout fails.
    """
    async with connection.cursor() as cursor:
        await cursor.execute("SET SESSION MAX_EXECUTION_TIME = 8000")   # 8s hard cap
        await cursor.execute("SET SESSION wait_timeout = 30")            # kill idle sessions fast


async def connect_to_mysql() -> None:
    """Create the primary pool (admin/health checks only — not used for queries anymore)."""
    host = _required_env("MYSQL_HOST")
    user = _required_env("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD", "")
    database = _required_env("MYSQL_DATABASE")

    try:
        port = int(os.getenv("MYSQL_PORT", "3306"))
        min_size = int(os.getenv("MYSQL_POOL_MIN_SIZE", "1"))
        max_size = int(os.getenv("MYSQL_POOL_MAX_SIZE", "10"))
    except ValueError as exc:
        raise MySQLConnectionError(
            "MYSQL_PORT, MYSQL_POOL_MIN_SIZE, and MYSQL_POOL_MAX_SIZE must be integers."
        ) from exc

    if min_size < 1 or max_size < min_size:
        raise MySQLConnectionError(
            "MYSQL_POOL_MIN_SIZE must be at least 1 and no greater than MYSQL_POOL_MAX_SIZE."
        )

    try:
        mysql.pool = await aiomysql.create_pool(
            host=host,
            port=port,
            user=user,
            password=password,
            db=database,
            minsize=min_size,
            maxsize=max_size,
            autocommit=True,
            connect_timeout=5,
        )
        async with mysql.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
    except (OSError, aiomysql.MySQLError) as exc:
        if mysql.pool is not None:
            mysql.pool.close()
            await mysql.pool.wait_closed()
            mysql.pool = None
        raise MySQLConnectionError(
            f"Could not connect to MySQL database '{database}': {exc}"
        ) from exc

    logger.info("Connected to MySQL primary database '%s' at %s:%d", database, host, port)


async def connect_to_read_replica() -> None:
    """
    NEW FUNCTION.
    Creates the read-only pool used by every query-execution node.
    Requires a separate, least-privilege DB user (SELECT-only grants) and,
    ideally, a physically separate replica host — never the primary.
    """
    host = _required_env("MYSQL_READ_REPLICA_HOST")
    user = _required_env("MYSQL_READ_ONLY_USER")
    password = os.getenv("MYSQL_READ_ONLY_PASSWORD", "")
    database = _required_env("MYSQL_READ_REPLICA_DATABASE")

    try:
        port = int(os.getenv("MYSQL_READ_REPLICA_PORT", "3306"))
        min_size = int(os.getenv("MYSQL_READ_POOL_MIN_SIZE", "1"))
        max_size = int(os.getenv("MYSQL_READ_POOL_MAX_SIZE", "10"))
    except ValueError as exc:
        raise MySQLConnectionError(
            "MYSQL_READ_REPLICA_PORT, MYSQL_READ_POOL_MIN_SIZE, MYSQL_READ_POOL_MAX_SIZE must be integers."
        ) from exc

    try:
        mysql.read_pool = await aiomysql.create_pool(
            host=host,
            port=port,
            user=user,
            password=password,
            db=database,
            minsize=min_size,
            maxsize=max_size,
            autocommit=True,
            connect_timeout=5,
            init_command=None,     # per-connection init runs via _init_session below
        )
        async with mysql.read_pool.acquire() as connection:
            await _init_session(connection)
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
    except (OSError, aiomysql.MySQLError) as exc:
        if mysql.read_pool is not None:
            mysql.read_pool.close()
            await mysql.read_pool.wait_closed()
            mysql.read_pool = None
        raise MySQLConnectionError(
            f"Could not connect to MySQL read replica '{database}': {exc}"
        ) from exc

    logger.info("Connected to MySQL READ REPLICA '%s' at %s:%d", database, host, port)


async def close_mysql_connection() -> None:
    """Close both pools during FastAPI shutdown."""
    if mysql.pool is not None:
        mysql.pool.close()
        await mysql.pool.wait_closed()
        mysql.pool = None
    if mysql.read_pool is not None:
        mysql.read_pool.close()
        await mysql.read_pool.wait_closed()
        mysql.read_pool = None
    logger.info("MySQL connection pools closed")


def get_pool() -> aiomysql.Pool:
    """Primary pool — admin/health checks only. Do not use for query execution."""
    if mysql.pool is None:
        raise MySQLNotConnectedError(
            "Database is not connected. Was connect_to_mysql() called on startup?"
        )
    return mysql.pool


def get_read_pool() -> aiomysql.Pool:
    """NEW. Read-replica pool — every query-execution node must use this, not get_pool()."""
    if mysql.read_pool is None:
        raise MySQLNotConnectedError(
            "Read replica is not connected. Was connect_to_read_replica() called on startup?"
        )
    return mysql.read_pool


async def health_check() -> bool:
    """Return whether a read-only connectivity check succeeds."""
    if mysql.pool is None:
        return False

    try:
        async with mysql.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                return await cursor.fetchone() == (1,)
    except (OSError, aiomysql.MySQLError):
        logger.exception("MySQL health check failed")
        return False


async def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Runs against the PRIMARY pool — kept for schema/admin reads only (e.g. startup schema fetch)."""
    pool_wait = 0.0
    query_fetch = 0.0
    row_count = None
    ok = False
    try:
        pool = get_pool()
        acquire_started = time.perf_counter()
        try:
            async with pool.acquire() as connection:
                pool_wait = time.perf_counter() - acquire_started
                async with connection.cursor() as cursor:
                    query_started = time.perf_counter()
                    try:
                        await cursor.execute(query, params)
                        rows = list(await cursor.fetchall())
                        row_count = len(rows)
                    finally:
                        query_fetch = time.perf_counter() - query_started
        finally:
            if pool_wait == 0.0:
                pool_wait = time.perf_counter() - acquire_started
        ok = True
        return rows
    finally:
        record_database_call(
            "primary_read", pool_wait, query_fetch, row_count, ok
        )


async def fetch_all_readonly(query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """
    NEW FUNCTION. Runs against the READ REPLICA pool.
    This is what run_self_fix.py (Node 8, execution) must call — never fetch_all().
    """
    pool_wait = 0.0
    query_fetch = 0.0
    row_count = None
    ok = False
    try:
        pool = get_read_pool()
        acquire_started = time.perf_counter()
        try:
            async with pool.acquire() as connection:
                pool_wait = time.perf_counter() - acquire_started
                async with connection.cursor() as cursor:
                    query_started = time.perf_counter()
                    try:
                        await cursor.execute(query, params)
                        rows = list(await cursor.fetchall())
                        row_count = len(rows)
                    finally:
                        query_fetch = time.perf_counter() - query_started
        finally:
            if pool_wait == 0.0:
                pool_wait = time.perf_counter() - acquire_started
        ok = True
        return rows
    finally:
        record_database_call(
            "read_replica_read", pool_wait, query_fetch, row_count, ok
        )


async def fetch_readonly_result(
    query: str,
    params: tuple[Any, ...] = (),
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Runs a validated SELECT on the read replica and includes result labels."""
    pool_wait = 0.0
    query_fetch = 0.0
    row_count = None
    ok = False
    acquire_started = time.perf_counter()
    try:
        pool = get_read_pool()
        async with pool.acquire() as connection:
            pool_wait = time.perf_counter() - acquire_started
            async with connection.cursor() as cursor:
                query_started = time.perf_counter()
                try:
                    await cursor.execute(query, params)
                    columns = [
                        str(description[0])
                        for description in (cursor.description or [])
                    ]
                    rows = list(await cursor.fetchall())
                    row_count = len(rows)
                finally:
                    query_fetch = time.perf_counter() - query_started
        ok = True
        return columns, rows
    finally:
        if pool_wait == 0.0:
            pool_wait = time.perf_counter() - acquire_started
        record_database_call(
            "read_replica_read", pool_wait, query_fetch, row_count, ok
        )