import pytest

from app.agent.graph import _zero_result_is_authoritative


@pytest.mark.parametrize(
    "question",
    [
        "Did any active shops have missing contact details?",
        "Are there any bookings without a verified shop?",
        "Find anomalies in the latest transactions.",
    ],
)
def test_negative_existence_and_anomaly_questions_preserve_empty_results(question):
    assert _zero_result_is_authoritative({}, question)


@pytest.mark.parametrize("operator", ["IS NULL", "IS MISSING", "NOT IN"])
def test_negative_filters_preserve_empty_results(operator):
    query = {
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {"table": "shop_details", "column": "name", "operator": operator}
            ],
        }
    }

    assert _zero_result_is_authoritative(query, "List matching shops")


def test_exclusion_contract_preserves_empty_results():
    query = {"exclude_entities_with_rows_in": [{"table": "reviews"}]}

    assert _zero_result_is_authoritative(query, "List shops")


def test_ordinary_empty_listing_can_still_receive_value_suggestions():
    assert not _zero_result_is_authoritative({}, "List shops in Nagercoil")
