from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import aiomysql
from dotenv import load_dotenv

load_dotenv()

ORACLE_SQL: dict[str, str] = {
    "O1": (
        "SELECT sd.name, sa.city FROM shop_details sd "
        "JOIN subcategories sc ON sc.id = sd.subcategory_id "
        "JOIN shop_address sa ON sa.shop_id = sd.id "
        "AND sa.is_default = 1 AND sa.city = 'Nagercoil' "
        "WHERE sd.status = 1 "
        "AND sc.subcategoryname = 'Drinks & Beverages' "
        "ORDER BY sd.name;"
    ),
    "O2": (
        "SELECT sd.name, sa.city FROM shop_details sd "
        "JOIN shop_address sa ON sa.shop_id = sd.id "
        "AND sa.is_default = 1 AND sa.city = 'Nagercoil' "
        "WHERE sd.status = 1 AND sd.name LIKE '%juice%' "
        "ORDER BY sd.name;"
    ),
    "O3": (
        "SELECT sd.name, sa.city FROM shop_details sd "
        "JOIN shop_address sa ON sa.shop_id = sd.id "
        "AND sa.is_default = 1 AND sa.city = 'Nagercoil' "
        "WHERE sd.status = 1 AND sd.name LIKE '%bakery%' "
        "ORDER BY sd.name;"
    ),
    "O4": (
        "SELECT COUNT(*) AS active_shop_count "
        "FROM shop_details WHERE status = 1;"
    ),
    "O5": (
        "SELECT DISTINCT s.name AS shop_name "
        "FROM shop_details s "
        "JOIN subcategories sc ON sc.id = s.subcategory_id "
        "WHERE s.status = 1 "
        "AND sc.subcategoryname IN "
        "('Snacks & Beverages', 'Drinks & Beverages');"
    ),
    "O6": (
        "SELECT time_slot_month AS booking_month, COUNT(*) AS booking_count "
        "FROM bookings WHERE status = 1 "
        "AND time_slot_date >= '2025-01-01' AND time_slot_date < '2026-01-01' "
        "GROUP BY time_slot_month "
        "HAVING COUNT(*) = ("
        "SELECT MAX(monthly.booking_count) FROM ("
        "SELECT COUNT(*) AS booking_count FROM bookings WHERE status = 1 "
        "AND time_slot_date >= '2025-01-01' AND time_slot_date < '2026-01-01' "
        "GROUP BY time_slot_month"
        ") AS monthly"
        ") ORDER BY booking_month;"
    ),
    "O7": (
        "SELECT s.name AS shop_name, SUM(b.final_amount) AS total_revenue "
        "FROM bookings b JOIN shop_details s ON s.id = b.shop_id "
        "WHERE b.status = 1 AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01' "
        "GROUP BY s.id, s.name ORDER BY total_revenue DESC LIMIT 5;"
    ),
    "O8": (
        "SELECT s.name AS shop_name, COUNT(*) AS review_count, "
        "ROUND(AVG(r.rating), 2) AS average_rating "
        "FROM reviews r JOIN shop_details s ON s.id = r.shop_id "
        "WHERE s.status = 1 GROUP BY s.id, s.name HAVING COUNT(*) > 2 "
        "ORDER BY review_count DESC, s.name;"
    ),
    "O9": "SELECT id, name FROM shop_details WHERE status = 1 LIMIT 5;",
}


def _db_kwargs() -> dict[str, Any]:
    required = {
        "host": "MYSQL_READ_REPLICA_HOST",
        "user": "MYSQL_READ_ONLY_USER",
        "db": "MYSQL_READ_REPLICA_DATABASE",
    }
    missing = [env_name for env_name in required.values() if not os.getenv(env_name)]
    if missing:
        raise RuntimeError(
            "Oracle queries require read-only database settings: " + ", ".join(missing)
        )

    return {
        "host": os.environ["MYSQL_READ_REPLICA_HOST"],
        "port": int(os.getenv("MYSQL_READ_REPLICA_PORT", "3306")),
        "user": os.environ["MYSQL_READ_ONLY_USER"],
        "password": os.getenv("MYSQL_READ_ONLY_PASSWORD", ""),
        "db": os.environ["MYSQL_READ_REPLICA_DATABASE"],
        "autocommit": True,
        "connect_timeout": 5,
    }


async def _fetch_oracle_rows(query: str) -> list[dict[str, Any]]:
    if not query.lstrip().upper().startswith("SELECT"):
        raise ValueError("Oracle SQL must be a read-only SELECT statement.")

    conn = await aiomysql.connect(**_db_kwargs())
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query)
            rows = await cur.fetchall()
            return [dict(row) for row in rows]
    finally:
        conn.close()


def _fetch_oracle_rows_in_worker(query: str) -> list[dict[str, Any]]:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_fetch_oracle_rows(query))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()


def fetch_oracle_case(case_id: str) -> list[dict[str, Any]]:
    try:
        query = ORACLE_SQL[case_id]
    except KeyError as exc:
        raise KeyError(f"Unknown oracle case: {case_id}") from exc

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_fetch_oracle_rows_in_worker, query).result()
