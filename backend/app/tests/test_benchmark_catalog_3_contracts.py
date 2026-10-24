from collections import Counter

from app.tests.golden_benchmark_catalog_3 import GOLDEN_BENCHMARK_CASES


def test_catalog_3_has_forty_consecutive_cases():
    numbers = [case["number"] for case in GOLDEN_BENCHMARK_CASES]

    assert len(GOLDEN_BENCHMARK_CASES) == 40
    assert numbers == list(range(26, 66))


def test_catalog_3_preserves_agreed_coverage_distribution():
    categories = Counter(case["category"] for case in GOLDEN_BENCHMARK_CASES)

    assert categories == {
        "classifier": 6,
        "comparison": 6,
        "summary": 4,
        "breakdown": 4,
        "ranking": 5,
        "follow_up": 6,
        "data_quality": 4,
        "edge_safety": 5,
    }


def test_non_sql_scenarios_have_runner_metadata():
    classifier_cases = [
        case for case in GOLDEN_BENCHMARK_CASES
        if case["case_type"] == "classifier"
    ]
    conversation_cases = [
        case for case in GOLDEN_BENCHMARK_CASES
        if case["case_type"] == "conversation"
    ]

    assert len(classifier_cases) == 6
    assert all(case["expected_intent"] in {"data", "chat"} for case in classifier_cases)
    assert all(case["expected_route"] in {"grade_schema", "return_save"} for case in classifier_cases)
    assert len(conversation_cases) == 6
    assert all(case["turns"] for case in conversation_cases)


def test_clarification_scenarios_expect_explicit_clarification():
    clarification_cases = [
        case for case in GOLDEN_BENCHMARK_CASES
        if case["expected_clarification"]
    ]

    assert [case["number"] for case in clarification_cases] == [54, 55, 56]


def test_catalog_3_uses_native_month_values_and_avoids_unasked_columns():
    cases_by_number = {case["number"]: case for case in GOLDEN_BENCHMARK_CASES}

    count_comparison = cases_by_number[32]
    assert "completed booking counts" in count_comparison["question"]
    assert "COUNT(CASE WHEN" in count_comparison["golden_sql"]
    assert "SUM(" not in count_comparison["golden_sql"]
    assert "2024-01-01" in count_comparison["golden_sql"]
    assert "2025-01-01" in count_comparison["golden_sql"]

    side_by_side_comparison = cases_by_number[33]
    assert "side by side" in side_by_side_comparison["question"]
    assert "AS booking_count_2025" in side_by_side_comparison["golden_sql"]
    assert "AS revenue_2025" in side_by_side_comparison["golden_sql"]
    assert "AS booking_count_2026" in side_by_side_comparison["golden_sql"]
    assert "AS revenue_2026" in side_by_side_comparison["golden_sql"]
    assert "GROUP BY YEAR" not in side_by_side_comparison["golden_sql"]

    month_case = cases_by_number[34]
    assert "time_slot_month" in month_case["golden_sql"]
    assert "DATE_FORMAT" not in month_case["golden_sql"]

    leaders_case = cases_by_number[37]
    assert "HAVING COUNT(*) = (" in leaders_case["golden_sql"]
    assert "MAX(monthly.booking_count)" in leaders_case["golden_sql"]
    assert leaders_case["golden_sql"].count("time_slot_date >= '2025-01-01'") == 2

    change_case = cases_by_number[35]
    assert "AS revenue_change" in change_case["golden_sql"]
    assert "AS revenue_2025" not in change_case["golden_sql"]
    assert "AS revenue_2026" not in change_case["golden_sql"]

    review_case = cases_by_number[39]
    assert "five_star_percentage" in review_case["golden_sql"]
    assert "review_count" not in review_case["golden_sql"].lower()

    state_case = cases_by_number[43]
    assert "GROUP BY state" in state_case["golden_sql"]
    assert "LOWER(state)" not in state_case["golden_sql"]

    ranking_case = cases_by_number[47]
    assert "SELECT s.name AS shop_name" in ranking_case["golden_sql"]
    assert "review_count" not in ranking_case["golden_sql"].lower()

    may_case = cases_by_number[52]
    assert "b.time_slot_month" in may_case["golden_sql"]
    assert "b.time_slot_date >= '2025-05-01'" in may_case["golden_sql"]
    assert "top five" in cases_by_number[51]["turns"][1]["question"].lower()
    assert all(
        column in cases_by_number[52]["turns"][1]["question"].lower()
        for column in ("id", "month", "scheduled date", "final amount")
    )
    assert "CAST(b.id AS CHAR) AS booking_id" in may_case["golden_sql"]
