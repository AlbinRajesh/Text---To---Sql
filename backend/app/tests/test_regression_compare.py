from app.tests.regression_benchmark import PASS_LABELS, _compare_results

GOLDEN = [
    {"c0": "A", "c1": "x", "c2": 1},
    {"c0": "A", "c1": "y", "c2": 2},
    {"c0": "B", "c1": "z", "c2": 3},
]


def test_fewer_columns_match_by_value():
    label, _ = _compare_results([{"shop_name": "A"}, {"shop_name": "B"}], GOLDEN)
    assert label == "PASS_PARTIAL_COLUMNS"
    assert label in PASS_LABELS


def test_unmatched_extra_column_is_ignored():
    generated = [{"id": 99, "name": "A"}, {"id": 98, "name": "B"}]
    label, _ = _compare_results(generated, GOLDEN)
    assert label == "PASS_PARTIAL_COLUMNS"


def test_missing_rows_still_fail():
    label, _ = _compare_results([{"name": "A"}], GOLDEN)
    assert label == "SUBSET"
    assert label not in PASS_LABELS


def test_unrelated_values_still_fail():
    label, _ = _compare_results([{"name": "Q"}], GOLDEN)
    assert label == "MISSING_COLUMNS"


def test_empty_generated_fails():
    label, _ = _compare_results([], GOLDEN)
    assert label == "ROW_COUNT_DIFF"