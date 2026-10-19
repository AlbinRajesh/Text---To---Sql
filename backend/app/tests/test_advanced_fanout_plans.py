"""The generator fan-out guard blocks unsafe advanced benchmark shapes."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output
from app.agent.nodes.safety_check import run_safety_check


SCHEMA = {
    "bookings": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "final_amount", "data_type": "decimal", "key": ""},
    ],
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "rating", "data_type": "decimal", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
        {"column": "category_id", "data_type": "bigint", "key": ""},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
    "categories": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "categoriesname", "data_type": "varchar", "key": ""},
    ],
    "offer": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "offer_heading", "data_type": "varchar", "key": ""},
    ],
}


def _edge(name, table, referenced_table, column, referenced_column="id"):
    return {
        "constraint_name": name,
        "table": table,
        "referenced_table": referenced_table,
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{
            "column": column,
            "referenced_column": referenced_column,
        }],
    }


RELATIONSHIPS = [
    _edge("reviewed_bookings_shop_id", "bookings", "shop_details", "shop_id"),
    _edge("reviewed_reviews_shop_id", "reviews", "shop_details", "shop_id"),
    _edge("reviewed_shop_address_shop_id", "shop_address", "shop_details", "shop_id"),
    _edge(
        "reviewed_shop_details_category_id",
        "shop_details",
        "categories",
        "category_id",
    ),
    _edge("reviewed_offer_shop_id", "offer", "shop_details", "shop_id"),
]


def _plan(table, joins, select_items, *, group_by=(), where_expression=None, having=None):
    return {
        "answerable": True,
        "reasoning": "t",
        "table": table,
        "joins": joins,
        "select_items": select_items,
        "where_expression": where_expression,
        "group_by": list(group_by),
        "having_expression": having,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }


def _relationship(constraint_name, table, referenced_table):
    return {
        "constraint_name": constraint_name,
        "table": table,
        "referenced_table": referenced_table,
    }


def test_case_3_dual_child_aggregates_are_rejected():
    plan = _plan(
        "bookings",
        [
            {
                "table": "shop_details",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_bookings_shop_id", "bookings", "shop_details"
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
        [
            {
                "table": "bookings",
                "column": "final_amount",
                "aggregate": "SUM",
                "distinct": False,
                "alias": "total_revenue",
            },
            {
                "table": "reviews",
                "column": "rating",
                "aggregate": "AVG",
                "distinct": False,
                "alias": "average_rating",
            },
        ],
    )
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert info.value.rule_id == "fanout_select_aggregate"


import copy

FLAGGED_RELATIONSHIPS = copy.deepcopy(RELATIONSHIPS)
for _edge_item in FLAGGED_RELATIONSHIPS:
    if _edge_item["constraint_name"] == "reviewed_shop_address_shop_id":
        _edge_item["single_row_when"] = {"column": "is_default", "value": 1}


def _review_average_with_address_plan(where_expression):
    return _plan(
        "reviews",
        [
            {
                "table": "shop_details",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_reviews_shop_id", "reviews", "shop_details"
                ),
            },
            {
                "table": "categories",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_shop_details_category_id",
                    "shop_details",
                    "categories",
                ),
            },
            {
                "table": "shop_address",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_shop_address_shop_id",
                    "shop_address",
                    "shop_details",
                ),
            },
        ],
        [
            {"table": "shop_address", "column": "city", "aggregate": None,
             "distinct": False, "alias": "city"},
            {"table": "reviews", "column": "rating", "aggregate": "AVG",
             "distinct": False, "alias": "average_rating"},
        ],
        group_by=[{"table": "shop_address", "column": "city"}],
        where_expression=where_expression,
    )


_DEFAULT_ADDRESS = {
    "table": "shop_address", "column": "is_default", "operator": "=", "value": 1,
}
_CATEGORY = {
    "table": "categories", "column": "categoriesname", "operator": "=",
    "value": "Transportation Services",
}


def test_case_8_review_average_with_flagged_address_child_is_accepted():
    plan = _review_average_with_address_plan(
        {"boolean": "AND", "conditions": [_DEFAULT_ADDRESS, _CATEGORY]}
    )
    result = _validate_llm_output(plan, SCHEMA, FLAGGED_RELATIONSHIPS, "q", [])
    assert result["table"] == "reviews"
    assert [join["table"] for join in result["joins"]] == [
        "shop_details",
        "categories",
        "shop_address",
    ]


def test_case_8_address_child_without_flag_is_still_rejected():
    plan = _review_average_with_address_plan(
        {"boolean": "AND", "conditions": [_DEFAULT_ADDRESS, _CATEGORY]}
    )
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert info.value.rule_id == "fanout_select_aggregate"


def test_default_address_join_condition_makes_numeric_aggregate_fanout_safe():
    plan = _review_average_with_address_plan(None)
    plan["joins"][2]["conditions"] = [_DEFAULT_ADDRESS]

    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        FLAGGED_RELATIONSHIPS,
        "Average review rating by default shop address.",
        [],
    )
    safety_result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=FLAGGED_RELATIONSHIPS,
    )

    assert "shop_address" in safety_result["sql"]


def test_flagged_address_join_with_filter_under_or_is_not_trusted():
    plan = _review_average_with_address_plan(
        {"boolean": "OR", "conditions": [_DEFAULT_ADDRESS, _CATEGORY]}
    )
    with pytest.raises(QueryGenerationError):
        _validate_llm_output(plan, SCHEMA, FLAGGED_RELATIONSHIPS, "q", [])

def test_case_9_review_average_joined_down_from_shop_is_rejected():
    plan = _plan(
        "shop_details",
        [
            {
                "table": "reviews",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_reviews_shop_id", "reviews", "shop_details"
                ),
            },
            {
                "table": "offer",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_offer_shop_id", "offer", "shop_details"
                ),
            },
        ],
        [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "distinct": False,
                "alias": "shop_name",
            },
            {
                "table": "reviews",
                "column": "rating",
                "aggregate": "AVG",
                "distinct": False,
                "alias": "average_rating",
            },
        ],
        group_by=[
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        where_expression={
            "table": "offer",
            "column": "offer_heading",
            "operator": "=",
            "value": "example",
        },
    )
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert info.value.rule_id == "fanout_select_aggregate"
