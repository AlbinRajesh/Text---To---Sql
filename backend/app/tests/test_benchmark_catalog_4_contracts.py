from app.tests.golden_benchmark_catalog_4 import (
    GOLDEN_BENCHMARK_CASES,
    MANUAL_DIAGNOSTIC_SCENARIOS,
)
from app.tests.master_golden_benchmark import GOLDEN_BENCHMARK_CASES as MASTER_CASES


def test_catalog_4_has_seven_consecutive_cases():
    numbers = [case["number"] for case in GOLDEN_BENCHMARK_CASES]

    assert numbers == list(range(66, 73))


def test_master_runner_includes_catalog_3_and_catalog_4_cases():
    numbers = [case["number"] for case in MASTER_CASES]

    assert numbers == list(range(26, 73))


def test_reported_project_failures_have_executable_regression_cases():
    cases_by_number = {case["number"]: case for case in GOLDEN_BENCHMARK_CASES}

    assert cases_by_number[66]["case_type"] == "conversation"
    assert cases_by_number[66]["turns"][0]["expected"] == "answer"
    assert "HAVING COUNT(*) = (" in cases_by_number[66]["golden_sql"]

    assert "a.is_default = 1" in cases_by_number[67]["golden_sql"]
    assert "a.is_default = 1" in cases_by_number[68]["golden_sql"]
    assert cases_by_number[69]["turns"][0]["expected"] == "answer"
    assert "GROUP BY p.id, p.name" in cases_by_number[70]["golden_sql"]
    assert cases_by_number[71]["case_type"] == "conversation"
    assert cases_by_number[72]["turns"][0]["question"].startswith(
        "Which active shops have more than two reviews?"
    )


def test_frontend_only_root_cause_probes_list_required_evidence():
    scenarios = {scenario["id"]: scenario for scenario in MANUAL_DIAGNOSTIC_SCENARIOS}

    assert set(scenarios) == {
        "clarification-yes-continuation",
        "pasted-sql-confirmation",
        "annual-total-summary",
    }
    assert all(scenario["steps"] and scenario["expected"] for scenario in scenarios.values())
    assert all(scenario["capture"] for scenario in scenarios.values())
