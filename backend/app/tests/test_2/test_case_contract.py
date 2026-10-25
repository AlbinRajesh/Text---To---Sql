import json
from copy import deepcopy

from app.tests.test_2.evaluators import evaluate_case
from app.tests.test_2.runner import CASE_FILE, REQUIRED_CASE_FIELDS, load_cases
from app.tests.test_2.runner import validate_test_catalog


def test_case_main_has_valid_unique_case_contract():
    cases = load_cases(CASE_FILE)
    assert cases
    for case in cases:
        assert REQUIRED_CASE_FIELDS <= case.keys()
        assert case["split"] in {"tuning", "exam"}
        assert case["compare_rules"].keys() >= {
            "order_matters",
            "extra_columns_allowed",
            "rounding_allowed",
        }
        if case["compare_rules"]["order_matters"]:
            assert case["compare_rules"].get("ordering_contract") in {
                "none",
                "ranking",
                "top_n",
                "latest",
            }


def test_case_main_has_seventy_thirty_split():
    cases = load_cases(CASE_FILE)
    assert len(cases) == 110
    assert sum(case["split"] == "tuning" for case in cases) == 77
    assert sum(case["split"] == "exam" for case in cases) == 33


def test_catalog_validation_marks_invalid_sql_without_dropping_other_cases():
    cases = load_cases()
    broken = deepcopy(cases[0])
    broken["id"] = "BROKEN_SQL"
    broken["golden_sql"] = (
        "SELECT sc.subcategoryname FROM subcategories sc "
        "JOIN shop_details sd sd.subcategory_id = sc.id"
    )
    valid = deepcopy(cases[1])

    validated = validate_test_catalog([broken, valid])

    assert len(validated) == 2
    assert validated[0]["_invalid_test_error"]
    assert "_invalid_test_error" not in validated[1]


def test_catalog_validation_rejects_unknown_ordering_contract(tmp_path):
    case = deepcopy(load_cases()[0])
    case["compare_rules"]["ordering_contract"] = "alphabetical"
    case_file = tmp_path / "invalid_ordering_contract_test.json"
    case_file.write_text(json.dumps([case]), encoding="utf-8")

    try:
        load_cases(case_file)
    except ValueError as exc:
        assert "ordering_contract" in str(exc)
    else:
        raise AssertionError("unknown ordering contract should be rejected")


def test_catalog_validation_rejects_unknown_tie_policy(tmp_path):
    case = deepcopy(load_cases()[0])
    case["compare_rules"]["tie_policy"] = "one_row_only"
    case_file = tmp_path / "invalid_tie_policy_test.json"
    case_file.write_text(json.dumps([case]), encoding="utf-8")

    try:
        load_cases(case_file)
    except ValueError as exc:
        assert "tie_policy" in str(exc)
    else:
        raise AssertionError("unknown tie_policy should be rejected")


def test_cutoff_tie_policy_requires_ranked_metric_column(tmp_path):
    case = deepcopy(load_cases()[0])
    case["compare_rules"].update({
        "ordering_contract": "top_n",
        "tie_policy": "cutoff_value_equivalence",
    })
    case["compare_rules"].pop("tie_metric_column", None)
    case_file = tmp_path / "missing_tie_metric_test.json"
    case_file.write_text(json.dumps([case]), encoding="utf-8")

    try:
        load_cases(case_file)
    except ValueError as exc:
        assert "tie_metric_column" in str(exc)
    else:
        raise AssertionError("cutoff tie evaluation should require its ranking metric")


def test_t012_oracle_includes_every_minimum_price_tie():
    case = next(case for case in load_cases() if case["id"] == "T012")

    assert case["compare_rules"]["tie_policy"] == "all_optimal_rows"
    assert "MIN(o2.offer_price)" in case["golden_sql"]
    assert "LIMIT 1" not in case["golden_sql"].upper()


def test_invalid_catalog_case_is_assigned_invalid_test_bucket():
    case = deepcopy(load_cases()[0])
    case["_invalid_test_error"] = "SQL parse failed"

    evaluation = evaluate_case(
        case,
        [],
        {"status": "not_run"},
        None,
        0.0,
    )

    assert evaluation["grading_bucket"] == "Invalid Test"
    assert evaluation["checks"]["catalog_validation"]["status"] == "invalid_test"
