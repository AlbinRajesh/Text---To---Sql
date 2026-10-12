"""ORDER BY in a grouped query must use a grouped column or a selected alias."""

import pytest

from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check

SCHEMA = {
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
RELATIONSHIPS = [{
    "constraint_name": "reviews_shop_fk",
    "table": "reviews",
    "referenced_table": "shop_details",
    "cardinality": "many_to_one",
    "verification": "declared_foreign_key",
    "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
}]
REL = {
    "constraint_name": "reviews_shop_fk",
    "table": "reviews",
    "referenced_table": "shop_details",
}


def _plan(order_by):
    return {
        "table": "shop_details",
        "joins": [{"table": "reviews", "join_type": "INNER", "relationship": REL}],
        "select_items": [{
            "table": "shop_details", "column": "name",
            "aggregate": None, "distinct": False, "alias": "shop_name",
        }],
        "where_expression": None,
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": order_by,
    }


def _run(order_by):
    return run_safety_check(_plan(order_by), list(SCHEMA), SCHEMA, RELATIONSHIPS)


def test_order_by_ungrouped_column_is_rejected():
    with pytest.raises(SafetyViolationError) as info:
        _run([{"table": "reviews", "column": "rating", "direction": "DESC"}])
    assert info.value.rule_id == "order_by_not_grouped"


def test_order_by_grouped_column_is_accepted():
    built = _run([{"table": "shop_details", "column": "name", "direction": "ASC"}])
    assert "ORDER BY" in built["sql"]


def test_order_by_selected_alias_is_accepted():
    built = _run([{"column": "shop_name", "direction": "ASC"}])
    assert "ORDER BY `shop_name` ASC" in built["sql"]


def test_plain_selected_column_missing_from_group_by_has_repairable_rule():
    query = {
        **_plan([]),
        "table": "reviews",
        "joins": [{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": REL,
        }],
        "select_items": [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "alias": "shop_name",
            },
            {
                "table": "reviews",
                "column": "rating",
                "aggregate": "AVG",
                "alias": "average_rating",
            },
        ],
        "group_by": [{"table": "shop_details", "column": "id"}],
    }

    with pytest.raises(SafetyViolationError) as info:
        run_safety_check(query, list(SCHEMA), SCHEMA, RELATIONSHIPS)

    assert info.value.rule_id == "group_by_selected_column"