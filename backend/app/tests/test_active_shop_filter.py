import pytest

from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _enforce_active_shop_filter,
    _validate_llm_output,
)
from app.agent.nodes.safety_check import _build_filter_expression


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


@pytest.mark.parametrize(
    ("question", "expected_where"),
    [
        ("List active offers.", None),
        ("List active offers from active shops.", _status_filter(1)),
    ],
)
def test_active_offer_status_policy_allows_requested_active_shop_filter(
    question, expected_where
):
    schema = {
        **SCHEMA,
        "offer": [
            {"column": "id", "key": "PRI"},
            {"column": "shop_id"},
            {"column": "status"},
        ],
    }
    relationship = {
        "constraint_name": "offer_shop_fk",
        "table": "offer",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    }
    plan = {
        "answerable": True,
        "reasoning": "List matching active offers.",
        "table": "shop_details",
        "joins": [{
            "table": "offer",
            "join_type": "INNER",
            "relationship": relationship,
            "conditions": [{
                "table": "offer",
                "column": "status",
                "operator": "=",
                "value": 1,
            }],
        }],
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
        schema,
        relationships=[relationship],
        question=question,
        enforce_active_shop_status=True,
    )

    assert normalized["where_expression"] == expected_where


@pytest.mark.parametrize(
    ("term", "expected_name_params"),
    [
        ("juice", ["%juice%"]),
        ("bakery", ["%bakery%"]),
        ("ju%ce", ["%ju\\%ce%"]),
        ("ju_ce", ["%ju\\_ce%"]),
        ("jo'h", ["%jo'h%"]),
    ],
)
def test_name_search_filter_uses_parameterized_word_matching(
    term, expected_name_params
):
    plan = {
        "answerable": True,
        "reasoning": "Search shop names.",
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
        question="show shops",
        catalog_filters=[{"kind": "name_search", "term": term}],
        enforce_active_shop_status=True,
    )

    sql, params = _build_filter_expression(
        normalized["where_expression"],
        ["shop_details"],
        SCHEMA,
    )

    assert "`shop_details`.`name` LIKE %s" in sql
    assert expected_name_params[0] in params
    assert term not in sql
