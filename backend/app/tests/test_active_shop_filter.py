import pytest

from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _enforce_active_shop_filter,
    _include_empty_text_values_for_missing_request,
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


def test_shop_discovery_without_requested_status_is_not_filtered():
    result = _enforce_active_shop_filter(None, {"shop_details"}, SCHEMA, "find shops")
    assert result is None


def test_active_status_is_added_outside_or_expressions():
    expression = {
        "boolean": "OR",
        "conditions": [
            {"table": "shop_details", "column": "name", "operator": "LIKE", "value": "%A%"},
            {"table": "shop_details", "column": "name", "operator": "LIKE", "value": "%B%"},
        ],
    }

    result = _enforce_active_shop_filter(
        expression, {"shop_details"}, SCHEMA, "find active shops named A or B"
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
        expression, {"shop_details"}, SCHEMA, "find active shops named A"
    )

    assert result["boolean"] == "AND"
    assert result["conditions"][0] == expression
    assert result["conditions"][1] == _status_filter(1)


def test_conflicting_status_filter_is_rejected():
    with pytest.raises(QueryGenerationError) as error:
        _enforce_active_shop_filter(
            _status_filter(0), {"shop_details"}, SCHEMA, "find active shops"
        )
    assert error.value.rule_id == "shop_status_conflict"


@pytest.mark.parametrize(
    ("question", "expected_status"),
    [("find active shops", 1), ("find inactive shops", 0)],
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


def test_joined_historical_people_query_does_not_get_an_implicit_active_filter():
    plan = {
        "answerable": True,
        "reasoning": "Find people with matching interactions.",
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
        "order_by": [],
    }

    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        question="Find people who left a low-rated review.",
        enforce_active_shop_status=True,
    )

    assert normalized["where_expression"] is None


def test_missing_text_request_matches_null_or_empty_string():
    schema = {
        "shop_details": [
            {"column": "phone", "data_type": "varchar"},
        ],
    }
    expression = {
        "table": "shop_details",
        "column": "phone",
        "operator": "IS NULL",
    }

    result = _include_empty_text_values_for_missing_request(
        expression, schema, "Find shops with no phone number listed."
    )

    assert result == {
        "boolean": "OR",
        "conditions": [
            expression,
            {
                "table": "shop_details",
                "column": "phone",
                "operator": "=",
                "value": "",
            },
        ],
    }


def test_missing_text_normalization_does_not_change_numeric_null_checks():
    schema = {"shops": [{"column": "rating", "data_type": "decimal"}]}
    expression = {"table": "shops", "column": "rating", "operator": "IS NULL"}

    assert _include_empty_text_values_for_missing_request(
        expression, schema, "Find shops with no rating listed."
    ) == expression


def test_people_name_projection_is_distinct():
    plan = {
        "answerable": True,
        "reasoning": "List people matching the interaction filters.",
        "table": "users",
        "joins": [],
        "select_items": [{
            "table": "users",
            "column": "name",
            "aggregate": None,
            "alias": "person_name",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "order_by": [],
    }

    normalized = _validate_llm_output(
        plan,
        {"users": [{"column": "id", "key": "PRI"}, {"column": "name"}]},
        question="Show me the names of people who left a low-rated review.",
    )

    assert normalized["distinct_rows"] is True


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
