import asyncio

from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
    fetch_all_readonly,
)


async def main() -> None:
    await connect_to_mysql()
    await connect_to_read_replica()
    try:
        rows = await fetch_all_readonly("SELECT CURRENT_USER() AS who")
        print("Read replica connected as:", rows)
    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    asyncio.run(main())