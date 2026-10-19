"""Frozen validation for the Step B suggestion evaluation cases.

Pass rules (fixed):
- should_match/edge row passes if at least one suggested type is in expected_types.
- no_match row passes only if NOTHING is suggested. A wrong suggestion is a hard failure.
- Only types with at least 1 active shop may be suggested.
- Overall bar: all 4 no_match rows pass AND at least 7 of the 8 should_match rows pass.
- The file and bar are frozen. They may not be edited after any method has been run.
"""

import asyncio
import csv
from pathlib import Path

import aiomysql
import pytest

from app.db.mysql_client import (
    MySQLConnectionError,
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
    fetch_all_readonly,
)


CASES_FILE = Path(__file__).with_name("step_b_suggestion_cases.csv")
EXPECTED_COLUMNS = ("word", "expected_types", "should_match", "group", "note")
EXPECTED_GROUP_COUNTS = {"should_match": 8, "no_match": 4, "edge": 3}
GENERIC_TYPES = {"all", "more", "other", "sports", "fitness", "accessories"}


def _read_cases():
    with CASES_FILE.open(encoding="utf-8", newline="") as cases_file:
        reader = csv.DictReader(cases_file)
        assert tuple(reader.fieldnames or ()) == EXPECTED_COLUMNS
        rows = list(reader)
    assert len(rows) == 15
    assert {
        group: sum(row["group"] == group for row in rows)
        for group in EXPECTED_GROUP_COUNTS
    } == EXPECTED_GROUP_COUNTS
    return rows


def test_step_b_cases_file_has_frozen_shape_and_consistent_expectations():
    rows = _read_cases()
    problems = []
    for row in rows:
        expected_types = [
            item.strip()
            for item in row["expected_types"].split("|")
            if item.strip()
        ]
        if row["group"] == "no_match":
            if row["expected_types"]:
                problems.append(f"{row['word']}: no_match row must have empty expected_types")
        elif not expected_types:
            problems.append(f"{row['word']}: {row['group']} row must have expected_types")
        if (row["group"] == "no_match") != (row["should_match"] == "no"):
            problems.append(f"{row['word']}: group and should_match disagree")
        if row["group"] != "no_match" and row["should_match"] != "yes":
            problems.append(f"{row['word']}: {row['group']} row must set should_match=yes")

    assert not problems, "\n".join(problems)


def test_step_b_expected_types_exist_and_have_active_shops():
    try:
        catalog_rows = asyncio.run(_read_catalog_and_active_counts())
    except (MySQLConnectionError, aiomysql.MySQLError, OSError) as exc:
        pytest.skip(f"Read-only database unavailable: {exc}")

    catalog = {}
    for source_table, type_name, active_count in catalog_rows:
        catalog.setdefault(type_name.casefold(), []).append(
            (source_table, int(active_count))
        )

    problems = []
    for row in _read_cases():
        for type_name in (
            item.strip()
            for item in row["expected_types"].split("|")
            if item.strip()
        ):
            matches = catalog.get(type_name.casefold(), [])
            if not matches:
                problems.append(
                    f"{row['word']} -> {type_name}: absent from categories/subcategories"
                )
                continue
            if not any(active_count >= 1 for _, active_count in matches):
                counts = ", ".join(
                    f"{source_table}={active_count}"
                    for source_table, active_count in matches
                )
                problems.append(
                    f"{row['word']} -> {type_name}: no active shops ({counts})"
                )
            if type_name.casefold() in GENERIC_TYPES:
                problems.append(
                    f"{row['word']} -> {type_name}: generic type is not allowed"
                )

    assert not problems, "\n".join(problems)


async def _read_catalog_and_active_counts():
    try:
        await connect_to_mysql()
        await connect_to_read_replica()
        return await fetch_all_readonly(
            """
            SELECT 'subcategories' AS source_table,
                   sc.subcategoryname AS type_name,
                   COUNT(DISTINCT sd.id) AS active_count
            FROM subcategories AS sc
            LEFT JOIN shop_details AS sd
              ON sd.subcategory_id = sc.id AND sd.status = 1
            WHERE sc.subcategoryname IS NOT NULL
              AND TRIM(sc.subcategoryname) <> ''
            GROUP BY sc.id, sc.subcategoryname
            UNION ALL
            SELECT 'categories' AS source_table,
                   c.categoriesname AS type_name,
                   COUNT(DISTINCT sd.id) AS active_count
            FROM categories AS c
            LEFT JOIN subcategories AS sc ON sc.category_id = c.id
            LEFT JOIN shop_details AS sd
              ON sd.subcategory_id = sc.id AND sd.status = 1
            WHERE c.categoriesname IS NOT NULL
              AND TRIM(c.categoriesname) <> ''
            GROUP BY c.id, c.categoriesname
            """
        )
    finally:
        await close_mysql_connection()
