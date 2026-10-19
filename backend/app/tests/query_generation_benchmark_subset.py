"""Run a subset of query_generation_benchmark's CASES by case number.

Does not modify query_generation_benchmark.py. Reuses its CASES list,
_run_case, and CSV writing logic exactly as-is.

Run from `backend` with:
    python -m app.tests.query_generation_benchmark_subset 2 4 5 6 7 11 12 15 17 18 22 23 24 25
"""

import argparse
import asyncio
import csv
import logging
import time
from pathlib import Path

from app.tests.query_generation_benchmark import (
    CASES,
    CSV_FIELDS,
    _run_case,
)
from app.agent.nodes.schema_grounding import get_cached_schema, refresh_schema_cache
from app.db.mysql_client import close_mysql_connection, connect_to_mysql
from app.services.embedding_service import build_schema_index

logger = logging.getLogger(__name__)


async def _run(case_numbers: list[int], output_path: Path) -> None:
    wanted = set(case_numbers)
    selected_cases = [c for c in CASES if c["number"] in wanted]
    missing = wanted - {c["number"] for c in selected_cases}
    if missing:
        raise SystemExit(f"No such case number(s) in CASES: {sorted(missing)}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    setup_started = time.perf_counter()
    await connect_to_mysql()
    try:
        await refresh_schema_cache()
        await build_schema_index(get_cached_schema())
        setup_ms = round((time.perf_counter() - setup_started) * 1000, 3)

        rows = []
        for case in selected_cases:
            logger.info("Benchmarking case %d/%d", case["number"], len(selected_cases))
            row = await _run_case(case)
            row["setup_ms"] = setup_ms
            rows.append(row)

        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        print(f"Wrote {len(rows)} diagnostic rows to {output_path}")
        print("Generated SQL was safety-validated but never executed.")
    finally:
        await close_mysql_connection()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=int, nargs="+", help="Case numbers to run, e.g. 2 5 11")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("query_generation_benchmark_retest.csv"),
        help="CSV destination (default: app/tests/query_generation_benchmark_retest.csv)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(args.cases, args.output.resolve()))


if __name__ == "__main__":
    main()