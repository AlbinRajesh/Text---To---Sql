"""Unique-user counts via users.id, with reviews as the base table, must be accepted."""

from app.agent.nodes.generate_query import _validate_llm_output
from app.agent.nodes.safety_check import run_safety_check

SCHEMA = {
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "user_id", "data_type": "bigint", "key": ""},
        {"column": "rating", "data_type": "decimal", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "subcategory_id", "data_type": "bigint", "key": ""},
    ],
    "subcategories": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "subcategoryname", "data_type": "varchar", "key": ""},
    ],
    "users": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
    ],
}


def _edge(name, table, referenced, column):
    return {
        "constraint_name": name,
        "table": table,
        "referenced_table": referenced,
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": column, "referenced_column": "id"}],
    }


RELATIONSHIPS = [
    _edge("reviewed_reviews_shop_id", "reviews", "shop_details", "shop_id"),
    _edge("reviewed_reviews_user_id", "reviews", "users", "user_id"),
    _edge("reviewed_shop_details_subcategory_id", "shop_details", "subcategories", "subcategory_id"),
]


def _join(table, name, owner, parent):
    return {
        "table": table,
        "join_type": "INNER",
        "relationship": {
            "constraint_name": name,
            "table": owner,
            "referenced_table": parent,
        },
    }


def test_unique_reviewers_per_subcategory_plan_is_accepted_end_to_end():
    plan = {
        "answerable": True,
        "reasoning": "t",
        "table": "reviews",
        "joins": [
            _join("shop_details", "reviewed_reviews_shop_id", "reviews", "shop_details"),
            _join("subcategories", "reviewed_shop_details_subcategory_id", "shop_details", "subcategories"),
            _join("users", "reviewed_reviews_user_id", "reviews", "users"),
        ],
        "select_items": [
            {"table": "subcategories", "column": "subcategoryname", "aggregate": None, "alias": "subcategory_name"},
            {"table": "reviews", "column": "rating", "aggregate": "AVG", "alias": "avg_rating"},
            {"table": "users", "column": "id", "aggregate": "COUNT", "distinct": True, "alias": "unique_users"},
        ],
        "where_expression": None,
        "group_by": [
            {"table": "subcategories", "column": "id"},
            {"table": "subcategories", "column": "subcategoryname"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": "users", "column": "id"},
        "order_by": [{"column": "avg_rating", "direction": "DESC"}],
        "limit": 1,
    }
    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS)
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    assert "COUNT(DISTINCT `users`.`id`)" in built["sql"]
    assert "AVG(`reviews`.`rating`)" in built["sql"]