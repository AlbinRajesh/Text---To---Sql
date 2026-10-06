"""A LEFT JOIN that nothing references means a condition was dropped."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output

SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "bookings": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
    ],
}
RELATIONSHIPS = [{
    "constraint_name": "reviewed_bookings_shop_id",
    "table": "bookings",
    "referenced_table": "shop_details",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
}]


def _plan(where):
    return {
        "answerable": True,
        "reasoning": "t",
        "table": "shop_details",
        "joins": [{
            "table": "bookings",
            "join_type": "LEFT",
            "relationship": {
                "constraint_name": "reviewed_bookings_shop_id",
                "table": "bookings",
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


def test_left_join_with_no_condition_is_rejected():
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(_plan(None), SCHEMA, RELATIONSHIPS)
    assert info.value.rule_id == "unused_left_join"


def test_left_join_used_by_is_null_is_accepted():
    where = {"table": "bookings", "column": "id", "operator": "IS NULL"}
    assert _validate_llm_output(_plan(where), SCHEMA, RELATIONSHIPS)