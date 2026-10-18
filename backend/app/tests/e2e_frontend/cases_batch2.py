from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import pytest

from app.tests.e2e_frontend.cases import CaseSpec
from app.tests.e2e_frontend.pages.chat_page import ChatPage
from app.tests.e2e_frontend.utils.oracle import _fetch_oracle_rows_in_worker


BATCH2_ORACLE_SQL: dict[str, str] = {
    "B2_O07": (
        "SELECT SUM(b.final_amount) AS total_revenue FROM bookings b "
        "WHERE b.status = 1 AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01';"
    ),
    "B2_O12": (
        "SELECT sd.id, sd.name FROM shop_details sd WHERE sd.status = 1 "
        "AND EXISTS (SELECT 1 FROM shop_address sa WHERE sa.shop_id = sd.id "
        "AND sa.is_default = 1 AND sa.city = 'Nagercoil') "
        "AND EXISTS (SELECT 1 FROM subcategories sc "
        "WHERE sc.id = sd.subcategory_id AND sc.subcategoryname = 'Fitness') "
        "ORDER BY sd.name LIMIT 300;"
    ),
    "B2_O14": (
        "SELECT COUNT(DISTINCT sd.id) AS active_shop_count "
        "FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id "
        "WHERE sd.status = 1 AND sa.is_default = 1 AND sa.city = 'Nagercoil';"
    ),
    "B2_O15": (
        "SELECT ranked.id, ranked.name, ranked.created_at FROM "
        "(SELECT sd.id, sd.name, sd.created_at, "
        "ROW_NUMBER() OVER (ORDER BY sd.created_at DESC, sd.id DESC) AS row_rank "
        "FROM shop_details sd WHERE sd.status = 1) AS ranked "
        "WHERE ranked.row_rank <= 5 ORDER BY ranked.row_rank;"
    ),
    "B2_O16": (
        "SELECT sd.id, sd.name FROM shop_details sd "
        "JOIN shop_address sa ON sa.shop_id = sd.id "
        "JOIN subcategories sc ON sc.id = sd.subcategory_id "
        "WHERE sd.status = 1 AND sa.is_default = 1 "
        "AND sa.city = 'Thiruvananthapuram' "
        "AND sc.subcategoryname = 'Restaurent' ORDER BY sd.name LIMIT 300;"
    ),
    "B2_O17": (
        "SELECT b.id, b.shop_id, b.created_at, b.final_amount FROM bookings b "
        "WHERE b.created_at >= '2025-01-01' AND b.created_at < '2026-01-01' "
        "ORDER BY b.created_at LIMIT 300;"
    ),
    "B2_O18": (
        "SELECT sa.city, COUNT(DISTINCT sd.id) AS active_shop_count "
        "FROM shop_details sd JOIN shop_address sa "
        "ON sa.shop_id = sd.id AND sa.is_default = 1 "
        "WHERE sd.status = 1 GROUP BY sa.city "
        "ORDER BY active_shop_count DESC, sa.city LIMIT 300;"
    ),
    "B2_O19": (
        "SELECT id, name FROM shop_details "
        "WHERE status = 1 AND name = 'No Such Shop 987654';"
    ),
    "B2_O21": (
        "SELECT sd.id, sd.name, (SELECT ROUND(AVG(r.rating), 2) "
        "FROM reviews r WHERE r.shop_id = sd.id) AS average_rating "
        "FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id "
        "AND sa.is_default = 1 WHERE sd.status = 1 AND sa.city = 'Nagercoil' "
        "AND (SELECT AVG(r.rating) FROM reviews r WHERE r.shop_id = sd.id) >= 4.0 "
        "ORDER BY average_rating DESC, sd.name LIMIT 300;"
    ),
    "B2_O22": (
        "SELECT c.categoriesname AS category_name, "
        "sc.subcategoryname AS subcategory_name, sd.name AS shop_name "
        "FROM shop_details sd JOIN categories c ON c.id = sd.category_id "
        "JOIN subcategories sc ON sc.id = sd.subcategory_id "
        "WHERE sd.status = 1 "
        "ORDER BY c.categoriesname, sc.subcategoryname, sd.name;"
    ),
    "B2_O22_ACTIVE": (
        "SELECT name FROM shop_details WHERE status = 1;"
    ),
    "B2_O23": (
        "SELECT id, shop_id, user_id, rating, created_at FROM reviews;"
    ),
    "B2_O24": (
        "SELECT s.name AS shop_name, COUNT(*) AS completed_bookings "
        "FROM bookings b JOIN shop_details s ON s.id = b.shop_id "
        "WHERE s.status = 1 AND b.status = 1 "
        "GROUP BY s.id, s.name "
        "ORDER BY completed_bookings DESC, s.name;"
    ),
    "B2_O25": (
        "SELECT sd.id, sd.name FROM shop_details sd WHERE sd.status = 1 "
        "AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.shop_id = sd.id) "
        "ORDER BY sd.name LIMIT 300;"
    ),
}


def fetch_batch2_oracle_case(case_id: str) -> list[dict[str, Any]]:
    try:
        query = BATCH2_ORACLE_SQL[case_id]
    except KeyError as exc:
        raise KeyError(f"Unknown batch 2 oracle case: {case_id}") from exc
    if not query.lstrip().upper().startswith("SELECT"):
        raise ValueError("Batch 2 oracle SQL must be a read-only SELECT statement.")
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_fetch_oracle_rows_in_worker, query).result()


CASES_BATCH2 = [
    CaseSpec(
        id="B2_Q07",
        phase="batch2",
        question="What was the total revenue from completed bookings in 2025?",
        scenario="batch2_total_revenue",
        oracle_id="B2_O07",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B2_Q11",
        phase="batch2",
        question="Show Bevereges",
        scenario="batch2_clarification",
    ),
    CaseSpec(
        id="B2_Q12",
        phase="batch2",
        question="Find fitnes businesses in Nagercoil",
        scenario="batch2_name_rows",
        oracle_id="B2_O12",
        ignore_columns=["id"],
    ),
    CaseSpec(
        id="B2_Q13",
        phase="batch2",
        question="Show one active drink business in Nagercoil.",
        scenario="batch2_o1_subset",
    ),
    CaseSpec(
        id="B2_Q14",
        phase="batch2",
        question="How many active shops have a default address in Nagercoil?",
        scenario="batch2_count",
        oracle_id="B2_O14",
    ),
    CaseSpec(
        id="B2_Q15",
        phase="batch2",
        question="Which five active shops were registered most recently?",
        scenario="batch2_ordered_names",
        oracle_id="B2_O15",
    ),
    CaseSpec(
        id="B2_Q16",
        phase="batch2",
        question="List active restaurant businesses in Thiruvananthapuram.",
        scenario="batch2_zero_rows",
        oracle_id="B2_O16",
    ),
    CaseSpec(
        id="B2_Q17",
        phase="batch2",
        question="Show bookings created during 2025.",
        scenario="batch2_ids_or_count",
        oracle_id="B2_O17",
    ),
    CaseSpec(
        id="B2_Q18",
        phase="batch2",
        question="How many active shops are in each city?",
        scenario="batch2_city_counts",
        oracle_id="B2_O18",
    ),
    CaseSpec(
        id="B2_Q19",
        phase="batch2",
        question="Is there an active shop named No Such Shop 987654?",
        scenario="batch2_zero_rows",
        oracle_id="B2_O19",
    ),
    CaseSpec(
        id="B2_Q20",
        phase="batch2",
        question="Show everything.",
        scenario="batch2_clarification",
    ),
    CaseSpec(
        id="B2_Q21",
        phase="batch2",
        question=(
            "Please list each active shop in Nagercoil that has an average review "
            "rating of at least 4.0, and show the shop name together with that "
            "average. Use the shop's default address to determine the city, include "
            "only ratings recorded for that shop, and return one row per qualifying "
            "shop. I am asking for a list of shops, not a count, comparison, or "
            "booking summary."
        ),
        scenario="batch2_name_rating_set",
        oracle_id="B2_O21",
    ),
    CaseSpec(
        id="B2_Q22",
        phase="batch2",
        question="Show all active shops with their category and subcategory names.",
        scenario="batch2_row_limit",
        oracle_id="B2_O22",
    ),
    CaseSpec(
        id="B2_Q23",
        phase="batch2",
        question="Show reviews.",
        scenario="batch2_ids_or_count",
        oracle_id="B2_O23",
    ),
    CaseSpec(
        id="B2_Q24",
        phase="batch2",
        question=(
            "Across all time, which active shops have at least one completed "
            "booking? Show each shop and how many completed bookings it has."
        ),
        scenario="batch2_name_count_set",
        oracle_id="B2_O24",
    ),
    CaseSpec(
        id="B2_Q25",
        phase="batch2",
        question="Which active shops have no reviews?",
        scenario="batch2_name_set",
        oracle_id="B2_O25",
    ),
]


def case_parameters_batch2():
    return [
        pytest.param(
            case,
            marks=[pytest.mark.e2e, pytest.mark.batch2],
            id=case.id,
        )
        for case in CASES_BATCH2
    ]


def _key(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).strip().lower())
    if normalized in {"shopname", "businessname", "shop", "business"}:
        return "name"
    if normalized in {"averagerating", "avgrating", "ratingaverage", "rating"}:
        return "rating"
    if normalized in {
        "activecount",
        "activeshopcount",
        "countofshops",
        "numberofshops",
        "shopcount",
        "totalshops",
        "count",
    }:
        return "count"
    if normalized in {
        "totalrevenue",
        "totalbookingrevenue",
        "totalcompletedbookingrevenue",
        "sum",
        "revenue",
    }:
        return "totalrevenue"
    if normalized in {"bookingcount", "completedbookings"}:
        return "count"
    if normalized in {"bookingid"}:
        return "id"
    return normalized


def _value(row: dict[str, Any], field: str) -> Any:
    for column, value in row.items():
        if _key(column) == field:
            return value
    raise AssertionError(
        f"Could not find {field!r} in app row columns {list(row)!r}"
    )


def _count_value(row: dict[str, Any]) -> Any:
    count_columns = [
        column
        for column in row
        if "count" in re.sub(r"[^a-z0-9]", "", str(column).strip().lower())
    ]
    if len(count_columns) == 1:
        return row[count_columns[0]]
    if len(count_columns) > 1:
        raise AssertionError(
            f"Multiple count columns found: {count_columns!r}; "
            f"available columns: {list(row)!r}"
        )

    numeric_columns = []
    for column, value in row.items():
        if _key(column) == "id":
            continue
        try:
            _decimal(value)
        except AssertionError:
            continue
        numeric_columns.append(column)
    if len(numeric_columns) == 1:
        return row[numeric_columns[0]]
    raise AssertionError(
        f"Expected one count or non-id numeric column; found "
        f"{numeric_columns!r} among columns {list(row)!r}"
    )


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise AssertionError(f"Expected a numeric value, got {value!r}") from exc
    assert number.is_finite(), f"Expected a finite numeric value, got {value!r}"
    return number


def _rows(events: list[dict[str, Any]], stream_rows: Callable) -> list[dict[str, Any]]:
    return list(stream_rows(events))


def _assert_no_table_rows(
    case: CaseSpec,
    events: list[dict[str, Any]],
    chat: ChatPage,
    stream_rows: Callable,
) -> None:
    assert _rows(events, stream_rows) == [], f"{case.id} returned stream rows"
    assert not chat.has_table(), f"{case.id} rendered a data table"


def run_batch2_case(
    case: CaseSpec,
    chat: ChatPage,
    backend_url: str,
    *,
    send: Callable,
    assert_status: Callable,
    stream_rows: Callable,
    fetch_existing: Callable,
) -> None:
    _, events = send(chat, backend_url, case.question)
    scenario = case.scenario

    if scenario == "batch2_clarification":
        result = assert_status(case, events, "clarification")
        _assert_no_table_rows(case, events, chat, stream_rows)
        assert not result.get("results"), f"{case.id} returned result data"
        return

    if scenario == "batch2_zero_rows":
        result = assert_status(case, events)
        assert result.get("status") in {"ok", "no_data", "unanswerable"}, (
            f"{case.id} returned unexpected status {result.get('status')!r}"
        )
        if case.id == "B2_Q19":
            actual = _rows(events, stream_rows)
            if not actual:
                assert not chat.has_table(), f"{case.id} rendered an empty data table"
                return
            assert len(actual) == 1, (
                f"{case.id} expected at most one zero-count row, got {len(actual)}"
            )
            row = actual[0]
            assert not any(_key(column) == "name" for column in row), (
                f"{case.id} returned a shop name: {row!r}"
            )
            numeric_values = []
            for value in row.values():
                try:
                    numeric_values.append(_decimal(value))
                except AssertionError:
                    continue
            assert len(numeric_values) == 1 and numeric_values[0] == 0, (
                f"{case.id} expected a single numeric zero count, got {row!r}"
            )
            return
        _assert_no_table_rows(case, events, chat, stream_rows)
        return

    if scenario == "batch2_o1_subset":
        result = assert_status(case, events, "ok")
        actual = _rows(events, stream_rows)
        allowed_rows = fetch_existing("O1")
        allowed_names = {str(_value(row, "name")).strip() for row in allowed_rows}
        assert len(actual) == 1, f"{case.id} expected exactly one row, got {len(actual)}"
        assert str(_value(actual[0], "name")).strip() in allowed_names
        return

    if case.id == "B2_Q12":
        result = assert_status(case, events)
        actual = _rows(events, stream_rows)
        if result.get("status") == "clarification":
            assert not actual, f"{case.id} returned data with clarification"
            assert not chat.has_table(), f"{case.id} rendered a table with clarification"
            return
        assert result.get("status") == "ok", (
            f"{case.id} expected clarification or ok, got {result.get('status')!r}"
        )
        expected = fetch_batch2_oracle_case("B2_O12")
        expected_names = {str(_value(row, "name")).strip() for row in expected}
        actual_names = {str(_value(row, "name")).strip() for row in actual}
        assert actual_names == expected_names
        return

    result = assert_status(case, events, "ok")
    actual = _rows(events, stream_rows)
    assert case.oracle_id is not None
    expected = fetch_batch2_oracle_case(case.oracle_id)

    if scenario == "batch2_total_revenue":
        assert len(expected) == 1 and len(actual) == 1
        assert _decimal(next(iter(expected[0].values()))) == _decimal(
            _value(actual[0], "totalrevenue")
        )
        return

    if scenario == "batch2_name_rows":
        assert len(actual) == len(expected)
        expected_names = {str(_value(row, "name")).strip() for row in expected}
        actual_names = {str(_value(row, "name")).strip() for row in actual}
        assert actual_names == expected_names
        return

    if scenario == "batch2_count":
        assert len(expected) == len(actual) == 1
        expected_count = _decimal(next(iter(expected[0].values())))
        assert expected_count == _decimal(_value(actual[0], "count"))
        return

    if scenario == "batch2_ordered_names":
        assert len(expected) == len(actual) == 5
        expected_names = [str(_value(row, "name")).strip() for row in expected]
        actual_names = [str(_value(row, "name")).strip() for row in actual]
        assert actual_names == expected_names
        return

    if scenario == "batch2_ids_or_count":
        assert len(actual) == len(expected)
        if actual and any(_key(column) == "id" for column in actual[0]):
            expected_ids = {_value(row, "id") for row in expected}
            actual_ids = {_value(row, "id") for row in actual}
            assert {_decimal(value) for value in actual_ids} == {
                _decimal(value) for value in expected_ids
            }
        return

    if scenario == "batch2_city_counts":
        def city_counts(rows):
            return {
                (
                    None if _value(row, "city") is None else str(_value(row, "city")).strip(),
                    int(_decimal(_count_value(row))),
                )
                for row in rows
            }

        actual_counts = city_counts(actual)
        expected_counts = city_counts(expected)
        app_only = sorted(actual_counts - expected_counts, key=lambda pair: str(pair[0]))
        oracle_only = sorted(expected_counts - actual_counts, key=lambda pair: str(pair[0]))
        assert actual_counts == expected_counts, (
            f"{case.id} city-count mismatch: app-only ({len(app_only)} pairs) "
            f"{app_only!r}; oracle-only ({len(oracle_only)} pairs) {oracle_only!r}"
        )
        return

    if scenario == "batch2_name_rating_set":
        def name_ratings(rows):
            return {
                (
                    str(_value(row, "name")).strip(),
                    _decimal(_value(row, "rating")),
                )
                for row in rows
            }

        assert name_ratings(actual) == name_ratings(expected)
        return

    if scenario == "batch2_row_limit":
        active_names = fetch_batch2_oracle_case("B2_O22_ACTIVE")
        allowed = {str(_value(row, "name")).strip() for row in active_names}
        actual_names = {
            str(_value(row, "name")).strip() for row in actual
        }
        assert len(expected) == 203
        assert len(actual) == 200
        assert actual_names <= allowed
        assert re.search(r"first 200 rows", chat.visible_text(), re.IGNORECASE), (
            f"{case.id} did not show the first-200-rows notice"
        )
        assert result.get("row_count") in {200, 203, None}
        return

    if scenario == "batch2_name_count_set":
        def name_counts(rows):
            return {
                (
                    str(_value(row, "name")).strip(),
                    int(_decimal(_count_value(row))),
                )
                for row in rows
            }

        assert name_counts(actual) == name_counts(expected)
        return

    if scenario == "batch2_name_set":
        expected_names = {str(_value(row, "name")).strip() for row in expected}
        actual_names = {str(_value(row, "name")).strip() for row in actual}
        assert len(expected) == 182
        assert actual_names == expected_names
        return

    raise AssertionError(f"Unknown batch 2 scenario {scenario!r} for {case.id}")
