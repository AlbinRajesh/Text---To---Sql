"""Phase 4: a joined shop_address must be filtered to the default address."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output

SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
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


def test_missing_default_filter_is_rejected():
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(_plan(None), SCHEMA, RELATIONSHIPS)
    assert info.value.rule_id == "address_requires_default"


def test_default_filter_alone_is_accepted():
    assert _validate_llm_output(_plan(DEFAULT), SCHEMA, RELATIONSHIPS)


def test_default_filter_inside_and_group_is_accepted():
    where = {"boolean": "AND", "conditions": [OTHER, DEFAULT]}
    assert _validate_llm_output(_plan(where), SCHEMA, RELATIONSHIPS)


def test_default_filter_under_or_is_rejected():
    where = {"boolean": "OR", "conditions": [DEFAULT, OTHER]}
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(_plan(where), SCHEMA, RELATIONSHIPS)
    assert info.value.rule_id == "address_requires_default"


def test_wrong_default_value_is_rejected():
    where = {**DEFAULT, "value": 0}
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(_plan(where), SCHEMA, RELATIONSHIPS)
    assert info.value.rule_id == "address_requires_default"


def test_missing_is_default_column_is_rejected():
    schema = {**SCHEMA, "shop_address": SCHEMA["shop_address"][:2]}
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(_plan(None), schema, RELATIONSHIPS)
    assert info.value.rule_id == "address_default_column_missing"
