"""A top-N parent query must not rank on an unselected one-to-many child value."""

import pytest

from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check


SCHEMA = {
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "rating", "data_type": "decimal", "key": ""},
    ],
}
RELATIONSHIPS = [
    {
        "constraint_name": "reviewed_shop_address_shop_id",
        "table": "shop_address",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_reviews_shop_id",
        "table": "reviews",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
]


def _relationship(name, table, referenced_table):
    return {
        "constraint_name": name,
        "table": table,
        "referenced_table": referenced_table,
    }


def _plan(*, select_reviews_rating=False, order_table="reviews", order_column="rating"):
    select_items = [
        {
            "table": "shop_address",
            "column": "city",
            "aggregate": None,
            "distinct": False,
            "alias": "location_city",
        }
    ]
    if select_reviews_rating:
        select_items.append({
            "table": "reviews",
            "column": "rating",
            "aggregate": None,
            "distinct": False,
            "alias": "rating_value",
        })
    return {
        "table": "shop_address",
        "joins": [
            {
                "table": "shop_details",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_shop_address_shop_id", "shop_address", "shop_details"
                ),
            },
            {
                "table": "reviews",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_reviews_shop_id", "reviews", "shop_details"
                ),
            },
        ],
        "select_items": select_items,
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "shop_address",
                    "column": "is_default",
                    "operator": "=",
                    "value": 1,
                },
                {
                    "table": "shop_address",
                    "column": "city",
                    "operator": "=",
                    "value": "Kazhakkoottam",
                },
            ],
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{
            "table": order_table,
            "column": order_column,
            "direction": "DESC",
        }],
        "limit": 1,
    }


def _run(plan):
    return run_safety_check(plan, list(SCHEMA), SCHEMA, RELATIONSHIPS)


def test_unselected_raw_child_rating_with_limit_is_rejected():
    with pytest.raises(SafetyViolationError) as info:
        _run(_plan())
    assert info.value.rule_id == "order_by_hidden_child_column"


def test_ordering_by_selected_child_value_remains_valid():
    result = _run(_plan(select_reviews_rating=True))
    assert "ORDER BY `reviews`.`rating` DESC" in result["sql"]


def test_ordering_by_selected_base_value_remains_valid():
    result = _run(_plan(order_table="shop_address", order_column="city"))
    assert "ORDER BY `shop_address`.`city` DESC" in result["sql"]
