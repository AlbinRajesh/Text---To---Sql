from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import pytest

from app.tests.e2e_frontend.cases import CaseSpec
from app.tests.e2e_frontend.pages.chat_page import ChatPage
from app.tests.e2e_frontend.utils.oracle import _fetch_oracle_rows_in_worker


# Shared SQL for the "highest-rated restaurants in Nagercoil" wording variants.
# The database spells the subcategory "Restaurent".
_HIGHEST_RATED_RESTAURANTS_NAGERCOIL_SQL = (
    "SELECT sd.name AS shop_name, sa.city AS city, "
    "COUNT(r.id) AS review_count, "
    "ROUND(AVG(r.rating), 2) AS average_rating "
    "FROM shop_details sd "
    "JOIN subcategories sc ON sc.id = sd.subcategory_id "
    "JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 "
    "JOIN reviews r ON r.shop_id = sd.id "
    "WHERE sd.status = 1 "
    "AND sc.subcategoryname = 'Restaurent' "
    "AND sa.city = 'Nagercoil' "
    "GROUP BY sd.id, sd.name, sa.city "
    "ORDER BY average_rating DESC, review_count DESC, sd.name LIMIT 300;"
)


BATCH4_ORACLE_SQL: dict[str, str] = {
    "B4_O01": _HIGHEST_RATED_RESTAURANTS_NAGERCOIL_SQL,
    "B4_O02": _HIGHEST_RATED_RESTAURANTS_NAGERCOIL_SQL,
    "B4_O03": _HIGHEST_RATED_RESTAURANTS_NAGERCOIL_SQL,
    "B4_O04": (
        "SELECT sd.name AS shop_name, o.actual_price AS actual_price, "
        "o.offer_price AS offer_price, "
        "(o.actual_price - o.offer_price) AS amount_saved "
        "FROM offer o "
        "JOIN shop_details sd ON sd.id = o.shop_id "
        "WHERE o.status = 1 AND sd.status = 1 "
        "ORDER BY amount_saved DESC, sd.name, o.id LIMIT 300;"
    ),
    "B4_O05": (
        "SELECT sd.name AS shop_name, "
        "COUNT(o.id) AS active_offer_count, "
        "ROUND(AVG((o.actual_price - o.offer_price) * 100.0 "
        "/ NULLIF(o.actual_price, 0)), 2) AS average_discount_percentage "
        "FROM shop_details sd "
        "JOIN offer o ON o.shop_id = sd.id "
        "WHERE sd.status = 1 AND o.status = 1 "
        "GROUP BY sd.id, sd.name "
        "ORDER BY average_discount_percentage DESC, sd.name LIMIT 5;"
    ),
    "B4_O06": (
        "SELECT sa.city AS city, COUNT(DISTINCT sd.id) AS shop_count "
        "FROM shop_details sd "
        "JOIN subcategories sc ON sc.id = sd.subcategory_id "
        "JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 "
        "WHERE sd.status = 1 AND sc.subcategoryname = 'Fitness' "
        "GROUP BY sa.city "
        "ORDER BY shop_count DESC, sa.city LIMIT 300;"
    ),
    "B4_O07": (
        "SELECT sd.name AS shop_name, sa.city AS city "
        "FROM shop_details sd "
        "JOIN subcategories sc ON sc.id = sd.subcategory_id "
        "JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 "
        "WHERE sd.status = 1 "
        "AND sc.subcategoryname = 'Restaurent' "
        "AND sa.city = 'Nagercoil' "
        "ORDER BY sd.name LIMIT 300;"
    ),
    "B4_O08": (
        "SELECT sd.name AS shop_name, sc.subcategoryname AS subcategory_name, "
        "COUNT(r.id) AS review_count, "
        "ROUND(AVG(r.rating), 2) AS average_rating "
        "FROM shop_details sd "
        "JOIN categories c ON c.id = sd.category_id "
        "JOIN subcategories sc ON sc.id = sd.subcategory_id "
        "JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 "
        "JOIN reviews r ON r.shop_id = sd.id "
        "WHERE sd.status = 1 "
        "AND c.categoriesname = 'Food & Dining' "
        "AND sa.city = 'Nagercoil' "
        "GROUP BY sd.id, sd.name, sc.id, sc.subcategoryname "
        "HAVING AVG(r.rating) >= 4 "
        "ORDER BY average_rating DESC, review_count DESC, sd.name LIMIT 300;"
    ),
    "B4_O09": (
        "SELECT b.city AS booking_city, COUNT(*) AS booking_count "
        "FROM bookings b "
        "WHERE b.status = 1 "
        "AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01' "
        "GROUP BY b.city "
        "ORDER BY booking_count DESC, b.city LIMIT 3;"
    ),
    "B4_O10": (
        "SELECT sd.name AS shop_name "
        "FROM shop_details sd "
        "WHERE sd.status = 1 "
        "AND EXISTS (SELECT 1 FROM offer o WHERE o.shop_id = sd.id AND o.status = 1) "
        "AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.shop_id = sd.id) "
        "ORDER BY sd.name LIMIT 100;"
    ),
    "B4_O11": (
        "SELECT c.categoriesname AS category_name, "
        "AVG(b.final_amount) AS average_final_amount "
        "FROM bookings b "
        "JOIN shop_details sd ON sd.id = b.shop_id "
        "JOIN categories c ON c.id = sd.category_id "
        "WHERE b.status = 1 AND sd.status = 1 "
        "GROUP BY c.id, c.categoriesname "
        "ORDER BY average_final_amount DESC, c.categoriesname LIMIT 300;"
    ),
    "B4_O12": (
        "SELECT sd.name AS shop_name, COUNT(*) AS booking_count "
        "FROM bookings b "
        "JOIN shop_details sd ON sd.id = b.shop_id "
        "WHERE sd.status = 1 AND b.status = 1 "
        "AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01' "
        "GROUP BY sd.id, sd.name "
        "HAVING COUNT(*) > 10 "
        "ORDER BY booking_count DESC, sd.name LIMIT 300;"
    ),
    "B4_O13": (
        "SELECT u.name AS reviewer_name, COUNT(r.id) AS review_count "
        "FROM reviews r "
        "JOIN users u ON u.id = r.user_id "
        "GROUP BY u.id, u.name "
        "ORDER BY review_count DESC, u.name LIMIT 5;"
    ),
    "B4_O14": (
        "SELECT c.categoriesname AS category_name, "
        "COUNT(o.id) AS active_offer_count "
        "FROM categories c "
        "LEFT JOIN shop_details sd ON sd.category_id = c.id AND sd.status = 1 "
        "LEFT JOIN offer o ON o.shop_id = sd.id AND o.status = 1 "
        "GROUP BY c.id, c.categoriesname "
        "ORDER BY active_offer_count DESC, c.categoriesname LIMIT 300;"
    ),
    "B4_O15": (
        "SELECT b.time_slot_month AS booking_month, "
        "SUM(b.final_amount) AS total_revenue "
        "FROM bookings b "
        "WHERE b.status = 1 "
        "AND b.time_slot_date >= '2025-01-01' "
        "AND b.time_slot_date < '2026-01-01' "
        "GROUP BY b.time_slot_month "
        "ORDER BY MIN(b.time_slot_date) LIMIT 300;"
    ),
    "B4_O16": (
        "SELECT MIN(o.offer_price) AS lowest_active_offer_price "
        "FROM offer o "
        "WHERE o.status = 1 LIMIT 300;"
    ),
    "B4_O17": (
        "SELECT COUNT(*) AS active_shop_count "
        "FROM shop_details sd "
        "WHERE sd.status = 1 LIMIT 300;"
    ),
    "B4_O18": (
        "SELECT sc.subcategoryname AS subcategory_name "
        "FROM subcategories sc "
        "ORDER BY sc.subcategoryname LIMIT 300;"
    ),
    "B4_O19": (
        "SELECT MAX(o.offer_price) AS highest_active_offer_price "
        "FROM offer o "
        "WHERE o.status = 1 LIMIT 300;"
    ),
    "B4_O20": (
        "SELECT COUNT(*) AS completed_booking_count "
        "FROM bookings b "
        "WHERE b.status = 1 LIMIT 300;"
    ),
}


CASES_BATCH4 = [
    # Fifteen moderate/difficult real-world questions, including wording variants and typos.
    CaseSpec(
        id="B4_Q01",
        phase="batch4",
        question="Find the highest-rated restaurants in Nagercoil",
        scenario="batch4_oracle_table",
        oracle_id="B4_O01",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B4_Q02",
        phase="batch4",
        question="Find the highest rated restaurants in Nagercoil",
        scenario="batch4_oracle_table",
        oracle_id="B4_O02",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B4_Q03",
        phase="batch4",
        question="Find the highest rateing restaurants in Nagercoil",
        scenario="batch4_oracle_table",
        oracle_id="B4_O03",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B4_Q04",
        phase="batch4",
        question="Find active offers and see how much each saves",
        scenario="batch4_oracle_table",
        oracle_id="B4_O04",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B4_Q05",
        phase="batch4",
        question="Which five active shops give the biggest average discount percentage on their active offers?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O05",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B4_Q06",
        phase="batch4",
        question="How many active fitness shops are there in each city?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O06",
    ),
    CaseSpec(
        id="B4_Q07",
        phase="batch4",
        question="List the active restaurants in Nagercoil",
        scenario="batch4_oracle_table",
        oracle_id="B4_O07",
    ),
    CaseSpec(
        id="B4_Q08",
        phase="batch4",
        question="Which Food & Dining shops in Nagercoil have an average rating of 4 or higher?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O08",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B4_Q09",
        phase="batch4",
        question="Which three cities had the most completed bookings scheduled in 2025?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O09",
    ),
    CaseSpec(
        id="B4_Q10",
        phase="batch4",
        question="Which active shops have active offers but no reviews yet?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O10",
    ),
    CaseSpec(
        id="B4_Q11",
        phase="batch4",
        question="What is the average amount of completed bookings for each category of active shops?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O11",
        normalize_screen_values=True,
    ),
    CaseSpec(
        id="B4_Q12",
        phase="batch4",
        question="Which active shops had more than 10 completed bookings scheduled in 2025? Show the booking count.",
        scenario="batch4_oracle_table",
        oracle_id="B4_O12",
    ),
    CaseSpec(
        id="B4_Q13",
        phase="batch4",
        question="Who are the five people who have written the most reviews? Show each review total.",
        scenario="batch4_oracle_table",
        oracle_id="B4_O13",
    ),
    CaseSpec(
        id="B4_Q14",
        phase="batch4",
        question="How many active offers does each category have across its active shops?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O14",
        ignore_columns=["category_id"],
    ),
    CaseSpec(
        id="B4_Q15",
        phase="batch4",
        question="What was the revenue from completed bookings in each month of 2025?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O15",
        normalize_screen_values=True,
    ),
    # Five simple lookups and scalar aggregates.
    CaseSpec(
        id="B4_Q16",
        phase="batch4",
        question="What is the lowest price among active offers?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O16",
    ),
    CaseSpec(
        id="B4_Q17",
        phase="batch4",
        question="How many active shops are there?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O17",
    ),
    CaseSpec(
        id="B4_Q18",
        phase="batch4",
        question="List all subcategory names in alphabetical order.",
        scenario="batch4_oracle_table",
        oracle_id="B4_O18",
    ),
    CaseSpec(
        id="B4_Q19",
        phase="batch4",
        question="What is the highest price among active offers?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O19",
    ),
    CaseSpec(
        id="B4_Q20",
        phase="batch4",
        question="How many completed bookings are there in total?",
        scenario="batch4_oracle_table",
        oracle_id="B4_O20",
    ),
]


def case_parameters_batch4():
    return [
        pytest.param(
            case,
            marks=[pytest.mark.e2e, pytest.mark.batch4],
            id=case.id,
        )
        for case in CASES_BATCH4
    ]


def fetch_batch4_oracle_case(case_id: str) -> list[dict[str, Any]]:
    try:
        query = BATCH4_ORACLE_SQL[case_id]
    except KeyError as exc:
        raise KeyError(f"Unknown batch 4 oracle case: {case_id}") from exc
    if not query.lstrip().upper().startswith("SELECT"):
        raise ValueError("Batch 4 oracle SQL must be a read-only SELECT statement.")
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_fetch_oracle_rows_in_worker, query).result()


def run_batch4_case(
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
    expected = fetch_batch4_oracle_case(case.oracle_id)

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