"""
test_mysql_client.py — manual smoke test for mysql_client.py

Run from backend/:
    python -m app.tests.test_mysql_client

Note: your mysql_client.py already calls load_dotenv() at the top of the
file, so this script doesn't need to call it again — it's covered as soon
as we import from that module.
"""

import asyncio

from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
    fetch_all,
    get_pool,
    health_check,
    MySQLConnectionError,
    MySQLNotConnectedError,
)


async def main() -> None:
    print("1. Confirming get_pool() fails BEFORE connecting (expected behavior)...")
    try:
        get_pool()
        print("   FAILED: expected MySQLNotConnectedError but none was raised.\n")
    except MySQLNotConnectedError as e:
        print(f"   Correctly raised MySQLNotConnectedError: {e}\n")

    print("2. Connecting to MySQL...")
    try:
        await connect_to_mysql()
        print("   Connected successfully.\n")
    except MySQLConnectionError as e:
        print(f"   FAILED to connect: {e}")
        return  # nothing else can run without a connection

    try:
        print("3. Running health check...")
        is_healthy = await health_check()
        print(f"   Healthy: {is_healthy}\n")

        print("4. Running a basic query (SELECT 1)...")
        rows = await fetch_all("SELECT 1")
        print(f"   Result: {rows}\n")

        print("5. Listing real tables in the connected database...")
        table_rows = await fetch_all("SHOW TABLES")
        table_names = [row[0] for row in table_rows]
        print(f"   Tables found: {table_names}\n")

        if not table_names:
            print("   WARNING: no tables found. Confirm MYSQL_DATABASE in .env matches "
                  "the schema you're using in MySQL Workbench.\n")
        else:
            print("6. Fetching a few real rows from the first table, to confirm real data access...")
            first_table = table_names[0]
            # Table name comes from SHOW TABLES (MySQL itself), not user input,
            # so it's safe to use directly here — this is a one-off manual test,
            # not the pattern the actual query-generation nodes should follow
            # (those must validate identifiers before use, since they involve
            # AI-generated table names).
            sample_rows = await fetch_all(f"SELECT * FROM `{first_table}` LIMIT 3")
            print(f"   Sample rows from '{first_table}': {sample_rows}\n")

        print("All checks passed.")

    finally:
        print("7. Closing connection...")
        await close_mysql_connection()
        print("   Closed.")


if __name__ == "__main__":
    asyncio.run(main())