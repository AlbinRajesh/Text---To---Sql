import pytest

from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _enforce_active_shop_filter,
    _validate_llm_output,
)


SCHEMA = {
    "shop_details": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
        {"column": "status"},
    ],
}


def _status_filter(value):
    return {
        "table": "shop_details",
        "column": "status",
        "operator": "=",
        "value": value,
    }


def test_shop_discovery_defaults_to_active_status():
    result = _enforce_active_shop_filter(None, {"shop_details"}, SCHEMA, "find shops")
    assert result == _status_filter(1)


def test_active_status_is_added_outside_or_expressions():
    expression = {
        "boolean": "OR",
        "conditions": [
            {"table": "shop_details", "column": "name", "operator": "LIKE", "value": "%A%"},
            {"table": "shop_details", "column": "name", "operator": "LIKE", "value": "%B%"},
        ],
    }

    result = _enforce_active_shop_filter(
        expression, {"shop_details"}, SCHEMA, "find shops named A or B"
    )

    assert result["boolean"] == "AND"
    assert result["conditions"][0] == expression
    assert result["conditions"][1] == _status_filter(1)


def test_explicit_inactive_shop_request_uses_inactive_status():
    result = _enforce_active_shop_filter(
        None, {"shop_details"}, SCHEMA, "find inactive shops"
    )
    assert result == _status_filter(0)


def test_status_filter_cannot_be_bypassed_inside_or():
    expression = {
        "boolean": "OR",
        "conditions": [_status_filter(1), {
            "table": "shop_details", "column": "name", "operator": "LIKE", "value": "%A%",
        }],
    }

    result = _enforce_active_shop_filter(
        expression, {"shop_details"}, SCHEMA, "find shops named A"
    )

    assert result["boolean"] == "AND"
    assert result["conditions"][0] == expression
    assert result["conditions"][1] == _status_filter(1)


def test_conflicting_status_filter_is_rejected():
    with pytest.raises(QueryGenerationError) as error:
        _enforce_active_shop_filter(
            _status_filter(0), {"shop_details"}, SCHEMA, "find shops"
        )
    assert error.value.rule_id == "shop_status_conflict"


@pytest.mark.parametrize(
    ("question", "expected_status"),
    [("find shops", 1), ("find inactive shops", 0)],
)
def test_query_validation_applies_shop_status_policy(question, expected_status):
    plan = {
        "answerable": True,
        "reasoning": "List matching shops.",
        "table": "shop_details",
        "joins": [],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        question=question,
        enforce_active_shop_status=True,
    )

    assert normalized["where_expression"] == _status_filter(expected_status)
