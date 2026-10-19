"""Phase 2: fan-out rejections carry a stable rule_id and stage."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output
from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check

SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "reviews": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "shop_id", "data_type": "int", "key": ""},
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


def test_generator_select_aggregate_rule_id():
    plan = {
        "answerable": True,
        "reasoning": "t",
        "table": "shop_details",
        "joins": [{"table": "reviews", "join_type": "INNER", "relationship": REL}],
        "select_items": [{
            "table": "reviews", "column": "rating",
            "aggregate": "AVG", "alias": "avg_rating",
        }],
        "group_by": [],
        "order_by": [],
    }
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert info.value.rule_id == "fanout_select_aggregate"
    assert info.value.stage == "generator"


def test_safety_having_aggregate_rule_id():
    plan = {
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
        "having_expression": {
            "aggregate": "AVG", "table": "reviews", "column": "rating",
            "operator": ">=", "value": 4.0,
        },
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    with pytest.raises(SafetyViolationError) as info:
        run_safety_check(plan, list(SCHEMA), SCHEMA, RELATIONSHIPS)
    assert info.value.rule_id == "fanout_having_aggregate"
    assert info.value.stage == "safety_check"
