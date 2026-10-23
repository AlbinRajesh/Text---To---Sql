"""Address filters are required only when a default/main address is requested."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output
from app.agent.nodes.safety_check import run_safety_check

SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
}
RELATIONSHIPS = [{
    "constraint_name": "reviewed_shop_address_shop_id",
    "table": "shop_address",
    "referenced_table": "shop_details",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
}]
DEFAULT = {
    "table": "shop_address", "column": "is_default", "operator": "=", "value": 1,
}
OTHER = {
    "table": "shop_address", "column": "id", "operator": ">", "value": 0,
}


def _plan(where):
    return {
        "answerable": True,
        "reasoning": "t",
        "table": "shop_details",
        "joins": [{
            "table": "shop_address",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_shop_address_shop_id",
                "table": "shop_address",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [{
            "table": "shop_details", "column": "id",
            "aggregate": "COUNT", "distinct": True, "alias": "shop_count",
        }],
        "where_expression": where,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": "shop_details", "column": "id"},
        "order_by": [],
    }


def test_any_matching_address_can_qualify_a_shop():
    assert _validate_llm_output(_plan(None), SCHEMA, RELATIONSHIPS)


def test_location_count_requires_default_address():
    location = {
        "table": "shop_address",
        "column": "city",
        "operator": "=",
        "value": "Nagercoil",
    }
    question = "How many shops are in Nagercoil?"
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(location), SCHEMA, RELATIONSHIPS, question=question
        )
    assert info.value.rule_id == "address_requires_default"

    normalized = _validate_llm_output(
        _plan({"boolean": "AND", "conditions": [location, DEFAULT]}),
        SCHEMA,
        RELATIONSHIPS,
        question=question,
    )
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    assert "COUNT(DISTINCT `shop_details`.`id`)" in built["sql"]
    assert "`shop_address`.`is_default` = %s" in built["sql"]
    assert "`shop_address`.`city` = %s" in built["sql"]
    assert "LOWER(" not in built["sql"].upper()
    assert "TRIM(" not in built["sql"].upper()


def test_grouped_location_count_requires_default_address():
    plan = _plan(DEFAULT)
    plan["group_by"] = [{"table": "shop_address", "column": "city"}]
    normalized = _validate_llm_output(
        plan, SCHEMA, RELATIONSHIPS, question="How many active shops are in each city?"
    )
    assert normalized["group_by"] == [
        {"table": "shop_address", "column": "city"}
    ]


def test_unscoped_distinct_count_keeps_any_address_behavior():
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(DEFAULT),
            SCHEMA,
            RELATIONSHIPS,
            question="How many shops are there?",
        )
    assert info.value.rule_id == "shop_count_address_scope"


def test_explicit_alternate_address_scope_allows_non_default_shop_count():
    location = {
        "table": "shop_address",
        "column": "city",
        "operator": "=",
        "value": "Nagercoil",
    }
    for question in (
        "Count shops in Nagercoil using all addresses.",
        "Count shops in Nagercoil using any address.",
        "Count shops by delivery address.",
    ):
        assert _validate_llm_output(
            _plan(location), SCHEMA, RELATIONSHIPS, question=question
        )


def test_non_count_address_join_keeps_default_address_guard():
    plan = _plan(None)
    plan["select_items"] = [{
        "table": "shop_details",
        "column": "name",
        "aggregate": None,
        "alias": "shop_name",
    }]
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            plan,
            SCHEMA,
            RELATIONSHIPS,
            question="List shops in Nagercoil.",
        )
    assert info.value.rule_id == "address_requires_default"


def test_explicit_default_address_request_requires_filter():
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(None),
            SCHEMA,
            RELATIONSHIPS,
            question="How many shops have a default address in this city?",
        )
    assert info.value.rule_id == "address_requires_default"


def test_default_filter_alone_is_accepted():
    question = "How many shops have a default address in this city?"
    assert _validate_llm_output(
        _plan(DEFAULT), SCHEMA, RELATIONSHIPS, question=question
    )


def test_default_filter_inside_and_group_is_accepted():
    where = {"boolean": "AND", "conditions": [OTHER, DEFAULT]}
    question = "How many shops have a default address in this city?"
    assert _validate_llm_output(
        _plan(where), SCHEMA, RELATIONSHIPS, question=question
    )


def test_default_filter_under_or_is_rejected():
    where = {"boolean": "OR", "conditions": [DEFAULT, OTHER]}
    question = "How many shops have a default address in this city?"
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(where), SCHEMA, RELATIONSHIPS, question=question
        )
    assert info.value.rule_id == "address_requires_default"


def test_default_filter_in_address_join_conditions_is_accepted():
    plan = _plan(None)
    plan["select_items"] = [{
        "table": "shop_details",
        "column": "name",
        "aggregate": None,
        "alias": "shop_name",
    }]
    plan["joins"][0]["conditions"] = [DEFAULT]
    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        RELATIONSHIPS,
        question="List shops in Nagercoil.",
    )
    assert normalized["joins"][0]["conditions"] == [DEFAULT]
def test_wrong_default_value_is_rejected():
    where = {**DEFAULT, "value": 0}
    question = "How many shops have a default address in this city?"
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(where), SCHEMA, RELATIONSHIPS, question=question
        )
    assert info.value.rule_id == "address_requires_default"


def test_missing_is_default_column_is_rejected():
    schema = {**SCHEMA, "shop_address": SCHEMA["shop_address"][:2]}
    question = "How many shops have a default address in this city?"
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(None), schema, RELATIONSHIPS, question=question
        )
    assert info.value.rule_id == "address_default_column_missing"
