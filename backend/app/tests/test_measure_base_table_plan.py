"""A measure-first plan (base = measured table, joins go up) must be accepted."""

from app.agent.nodes.generate_query import _validate_llm_output

SCHEMA = {
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
    "categories": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "categoriesname", "data_type": "varchar", "key": ""},
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
    _edge("reviewed_shop_details_category_id", "shop_details", "categories", "category_id"),
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


def test_average_rating_per_shop_with_reviews_as_base_is_accepted():
    plan = {
        "answerable": True,
        "reasoning": "t",
        "table": "reviews",
        "joins": [
            _join("shop_details", "reviewed_reviews_shop_id", "reviews", "shop_details"),
            _join("categories", "reviewed_shop_details_category_id", "shop_details", "categories"),
        ],
        "select_items": [
            {"table": "shop_details", "column": "name", "aggregate": None, "alias": "shop_name"},
            {"table": "categories", "column": "categoriesname", "aggregate": None, "alias": "category_name"},
            {"table": "reviews", "column": "rating", "aggregate": "AVG", "alias": "avg_rating"},
        ],
        "where_expression": None,
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
            {"table": "categories", "column": "id"},
            {"table": "categories", "column": "categoriesname"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"column": "avg_rating", "direction": "DESC"}],
    }
    assert _validate_llm_output(plan, SCHEMA, RELATIONSHIPS)