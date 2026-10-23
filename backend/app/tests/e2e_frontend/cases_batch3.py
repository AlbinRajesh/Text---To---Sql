from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import pytest

from app.tests.e2e_frontend.cases import CaseSpec
from app.tests.e2e_frontend.pages.chat_page import ChatPage
from app.tests.e2e_frontend.utils.oracle import _fetch_oracle_rows_in_worker


BATCH3_ORACLE_SQL: dict[str, str] = {
    "B3_O01": (
        "SELECT c.categoriesname AS category_name, "
        "COUNT(DISTINCT sd.id) AS shop_count "
        "FROM shop_details sd "
        "JOIN categories c ON c.id = sd.category_id "
        "WHERE sd.status = 1 "
        "GROUP BY c.id, c.categoriesname "
        "ORDER BY shop_count DESC, c.categoriesname LIMIT 300;"
    ),
    "B3_O02": (
        "SELECT sc.subcategoryname AS subcategory_name, "
        "COUNT(DISTINCT sd.id) AS shop_count "
        "FROM shop_details sd "
        "JOIN subcategories sc ON sc.id = sd.subcategory_id "
        "WHERE sd.status = 1 "
        "GROUP BY sc.id, sc.subcategoryname "
        "HAVING COUNT(DISTINCT sd.id) >= 5 "
        "ORDER BY shop_count DESC, sc.subcategoryname LIMIT 300;"
    ),
    "B3_O03": (
        "SELECT b.status AS booking_status, COUNT(*) AS booking_count, "
        "AVG(b.final_amount) AS average_final_amount "
        "FROM bookings b "
        "GROUP BY b.status "
        "ORDER BY b.status LIMIT 300;"
    ),
    "B3_O04": (
        "SELECT c.categoriesname AS category_name, "
        "SUM(b.final_amount) AS total_revenue "
        "FROM bookings b "
        "JOIN shop_details sd ON sd.id = b.shop_id "
        "JOIN categories c ON c.id = sd.category_id "
        "WHERE sd.status = 1 AND b.status = 1 "
        "AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01' "
        "GROUP BY c.id, c.categoriesname "
        "ORDER BY total_revenue DESC, c.categoriesname LIMIT 300;"
    ),
    "B3_O05": (
        "SELECT c.categoriesname AS category_name, "
        "COUNT(DISTINCT sd.id) AS shops_with_offers "
        "FROM shop_details sd "
        "JOIN categories c ON c.id = sd.category_id "
        "JOIN offer o ON o.shop_id = sd.id "
        "WHERE sd.status = 1 "
        "GROUP BY c.id, c.categoriesname "
        "ORDER BY shops_with_offers DESC, c.categoriesname LIMIT 300;"
    ),
    "B3_O06": (
        "SELECT sc.subcategoryname AS subcategory_name, "
        "COUNT(DISTINCT r.user_id) AS reviewer_count "
        "FROM subcategories sc "
        "LEFT JOIN shop_details sd ON sd.subcategory_id = sc.id AND sd.status = 1 "
        "LEFT JOIN reviews r ON r.shop_id = sd.id "
        "GROUP BY sc.id, sc.subcategoryname "
        "ORDER BY reviewer_count DESC, sc.subcategoryname LIMIT 300;"
    ),
    "B3_O07": (
        "SELECT COUNT(*) AS active_shops_without_bookings "
        "FROM shop_details sd "
        "WHERE sd.status = 1 "
        "AND NOT EXISTS ("
        "SELECT 1 FROM bookings b WHERE b.shop_id = sd.id"
        ") LIMIT 300;"
    ),
    "B3_O08": (
        "SELECT a.city AS booking_city, COUNT(*) AS booking_count "
        "FROM bookings b "
        "JOIN shop_details s ON s.id = b.shop_id "
        "JOIN shop_address a ON a.shop_id = s.id "
        "WHERE b.status = 1 AND a.is_default = 1 "
        "GROUP BY a.city "
        "HAVING COUNT(*) >= 1 "
        "ORDER BY booking_count DESC, a.city LIMIT 300;"
    ),
    "B3_O09": (
        "SELECT sd.name AS shop_name, c.categoriesname AS category_name, "
        "r.rating AS rating, r.created_at AS review_date "
        "FROM reviews r "
        "JOIN shop_details sd ON sd.id = r.shop_id "
        "JOIN categories c ON c.id = sd.category_id "
        "WHERE sd.status = 1 AND r.rating >= 4.0 "
        "AND r.created_at >= '2025-01-01 00:00:00' "
        "AND r.created_at < '2026-01-01 00:00:00' "
        "ORDER BY r.created_at DESC, sd.name LIMIT 100;"
    ),
    "B3_O10": (
        "SELECT a.city AS booking_city, "
        "SUM(b.final_amount) AS total_revenue "
        "FROM bookings b "
        "JOIN shop_details s ON s.id = b.shop_id "
        "JOIN shop_address a ON a.shop_id = s.id "
        "WHERE b.status = 1 "
        "AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01' "
        "AND a.is_default = 1 "
        "GROUP BY a.city "
        "ORDER BY total_revenue DESC, a.city LIMIT 5;"
    ),
    "B3_O11": (
        "SELECT sd.name AS shop_name, b.time_slot_month AS booking_month, "
        "COUNT(*) AS completed_booking_count "
        "FROM bookings b "
        "JOIN shop_details sd ON sd.id = b.shop_id "
        "WHERE sd.status = 1 AND b.status = 1 "
        "AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01' "
        "GROUP BY sd.id, sd.name, b.time_slot_month "
        "ORDER BY completed_booking_count DESC, sd.name, b.time_slot_month "
        "LIMIT 100;"
    ),
    "B3_O12": (
        "SELECT u.name AS reviewer_name, "
        "COUNT(DISTINCT c.id) AS category_count "
        "FROM reviews r "
        "JOIN users u ON u.id = r.user_id "
        "JOIN shop_details sd ON sd.id = r.shop_id "
        "JOIN categories c ON c.id = sd.category_id "
        "WHERE sd.status = 1 "
        "GROUP BY u.id, u.name "
        "HAVING COUNT(DISTINCT c.id) > 1 "
        "ORDER BY category_count DESC, u.name LIMIT 100;"
    ),
    "B3_O13": (
        "SELECT c.categoriesname AS category_name, "
        "COUNT(DISTINCT sa.city) AS unique_city_count "
        "FROM categories c "
        "LEFT JOIN shop_details sd ON sd.category_id = c.id AND sd.status = 1 "
        "LEFT JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 "
        "GROUP BY c.id, c.categoriesname "
        "ORDER BY unique_city_count DESC, c.categoriesname LIMIT 300;"
    ),
    "B3_O14": (
        "SELECT sc.subcategoryname AS subcategory_name, "
        "COUNT(DISTINCT o.id) AS active_offer_count "
        "FROM subcategories sc "
        "LEFT JOIN shop_details sd ON sd.subcategory_id = sc.id AND sd.status = 1 "
        "LEFT JOIN offer o ON o.shop_id = sd.id AND o.status = 1 "
        "GROUP BY sc.id, sc.subcategoryname "
        "ORDER BY active_offer_count DESC, sc.subcategoryname LIMIT 300;"
    ),
    "B3_O15": (
        "SELECT sd.name AS shop_name, COUNT(r.id) AS review_count "
        "FROM reviews r "
        "JOIN shop_details sd ON sd.id = r.shop_id "
        "WHERE sd.status = 1 "
        "AND r.created_at >= '2025-01-01 00:00:00' "
        "AND r.created_at < '2026-01-01 00:00:00' "
        "GROUP BY sd.id, sd.name "
        "ORDER BY review_count DESC, sd.name LIMIT 5;"
    ),
    "B3_O16": (
        "SELECT c.categoriesname AS category_name "
        "FROM categories c "
        "ORDER BY c.categoriesname LIMIT 300;"
    ),
    "B3_O17": (
        "SELECT COUNT(*) AS subcategory_count "
        "FROM subcategories LIMIT 300;"
    ),
    "B3_O18": (
        "SELECT sd.name AS shop_name "
        "FROM shop_details sd "
        "WHERE sd.status = 1 "
        "ORDER BY sd.name LIMIT 5;"
    ),
    "B3_O19": (
        "SELECT COUNT(*) AS review_count "
        "FROM reviews LIMIT 300;"
    ),
    "B3_O20": (
        "SELECT AVG(r.rating) AS average_rating "
        "FROM reviews r LIMIT 300;"
    ),
}


CASES_BATCH3 = [
    # Fifteen moderate/difficult plans: joins, grouping, date filters, HAVING, and ranking.
    CaseSpec(
        id="B3_Q01",
        phase="batch3",
        question="Which categories contain active shops, and how many shops are in each category?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O01",
    ),
    CaseSpec(
        id="B3_Q02",
        phase="batch3",
        question="Which subcategories have at least five active shops? Show the subcategory and shop count.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O02",
    ),
    CaseSpec(
        id="B3_Q03",
        phase="batch3",
        question="For each booking status, how many bookings are there and what is their average final amount?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O03",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B3_Q04",
        phase="batch3",
        question="For each category, what was the revenue from completed bookings scheduled in 2025?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O04",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B3_Q05",
        phase="batch3",
        question="Which categories have active shops that posted offers? Show the number of those shops per category.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O05",
    ),
    CaseSpec(
        id="B3_Q06",
        phase="batch3",
        question="How many distinct people reviewed active shops in each subcategory?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O06",
        column_map={"reviewer_count": "distinct_user_count"},
        ignore_columns=["subcategory_id"],
    ),
    CaseSpec(
        id="B3_Q07",
        phase="batch3",
        question="How many active shops have never received a booking?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O07",
    ),
    CaseSpec(
        id="B3_Q08",
        phase="batch3",
        question="Which cities have at least one completed booking? Show each booking city and its count.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O08",
    ),
    CaseSpec(
        id="B3_Q09",
        phase="batch3",
        question="List active-shop reviews rated at least 4 stars and recorded in 2025, with the shop name, category name, rating and review date.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O09",
        value_match={"review_date": "datetime"},
    ),
    CaseSpec(
        id="B3_Q10",
        phase="batch3",
        question="Which five booking cities had the highest revenue from completed bookings scheduled in 2025?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O10",
    ),
    CaseSpec(
        id="B3_Q11",
        phase="batch3",
        question="Show the 100 active-shop and month pairs with the most completed bookings scheduled in 2025.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O11",
    ),
    CaseSpec(
        id="B3_Q12",
        phase="batch3",
        question="Which people reviewed active shops in more than one category? Show each person and the number of distinct categories.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O12",
    ),
    CaseSpec(
        id="B3_Q13",
        phase="batch3",
        question="How many different default-address cities have active shops in each category?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O13",
        column_map={"unique_city_count": "city_count"},
        ignore_columns=["category_id"],
    ),
    CaseSpec(
        id="B3_Q14",
        phase="batch3",
        question="For each subcategory, how many active offers belong to active shops?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O14",
        ignore_columns=["subcategory_id"]
    ),
    CaseSpec(
        id="B3_Q15",
        phase="batch3",
        question="Which five active shops received the most reviews during 2025? Show each review total.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O15",
    ),
    # Five simple lookups and scalar aggregates.
    CaseSpec(
        id="B3_Q16",
        phase="batch3",
        question="List all category names in alphabetical order.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O16",
    ),
    CaseSpec(
        id="B3_Q17",
        phase="batch3",
        question="How many subcategories are listed?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O17",
    ),
    CaseSpec(
        id="B3_Q18",
        phase="batch3",
        question="Show the first five active shop names alphabetically.",
        scenario="batch3_oracle_table",
        oracle_id="B3_O18",
    ),
    CaseSpec(
        id="B3_Q19",
        phase="batch3",
        question="How many reviews are recorded in total?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O19",
    ),
    CaseSpec(
        id="B3_Q20",
        phase="batch3",
        question="What is the average rating across all recorded reviews?",
        scenario="batch3_oracle_table",
        oracle_id="B3_O20",
    ),
]


def case_parameters_batch3():
    return [
        pytest.param(
            case,
            marks=[pytest.mark.e2e, pytest.mark.batch3],
            id=case.id,
        )
        for case in CASES_BATCH3
    ]


def fetch_batch3_oracle_case(case_id: str) -> list[dict[str, Any]]:
    try:
        query = BATCH3_ORACLE_SQL[case_id]
    except KeyError as exc:
        raise KeyError(f"Unknown batch 3 oracle case: {case_id}") from exc
    if not query.lstrip().upper().startswith("SELECT"):
        raise ValueError("Batch 3 oracle SQL must be a read-only SELECT statement.")
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_fetch_oracle_rows_in_worker, query).result()


def run_batch3_case(
    case: CaseSpec,
    chat: ChatPage,
    backend_url: str,
    *,
    send: Callable,
    assert_status: Callable,
    stream_rows: Callable,
    compare_rows: Callable,
) -> None:
    _, events = send(chat, backend_url, case.question)
    result = assert_status(case, events)
    actual = list(stream_rows(events))
    assert case.oracle_id is not None
    expected = fetch_batch3_oracle_case(case.oracle_id)

    if not expected:
        assert result.get("status") in {"ok", "no_data"}, (
            f"{case.id} returned unexpected status {result.get('status')!r}"
        )
        assert not actual, f"{case.id} returned rows although its oracle is empty"
        assert not chat.has_table(), f"{case.id} rendered an empty result table"
        return

    assert result.get("status") == "ok", (
        f"{case.id} returned unexpected status {result.get('status')!r}; "
        f"full result: {result!r}"
    )
    if len(expected) == 1 and len(expected[0]) == 1:
        compare_rows(case, expected, actual, "stream")
        assert not chat.has_table(), (
            f"{case.id} should render a scalar answer, not a table"
        )
        return

    chat.wait_for_table()
    chat.expand_all_rows()
    compare_rows(case, expected, actual, "stream")
    compare_rows(case, expected, chat.table_rows(), "screen")
