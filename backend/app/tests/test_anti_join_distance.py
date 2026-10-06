"""IS NULL anti-join is only valid on a table joined directly to the base."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output

SCHEMA = {
    "categories": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "categoriesname", "data_type": "varchar", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "category_id", "data_type": "bigint", "key": ""},
    ],
    "offer": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
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
    _edge("shop_category", "shop_details", "categories", "category_id"),
    _edge("offer_shop", "offer", "shop_details", "shop_id"),
]


def _join(table, name, owner, parent):
    return {
        "table": table,
        "join_type": "LEFT",
        "relationship": {"constraint_name": name, "table": owner, "referenced_table": parent},
    }


def _plan(where, having):
    return {
        "answerable": True,
        "reasoning": "t",
        "table": "categories",
        "joins": [
            _join("shop_details", "shop_category", "shop_details", "categories"),
            _join("offer", "offer_shop", "offer", "shop_details"),
        ],
        "select_items": [{
            "table": "categories", "column": "categoriesname",
            "aggregate": None, "alias": "category_name",
        }],
        "where_expression": where,
        "group_by": [
            {"table": "categories", "column": "id"},
            {"table": "categories", "column": "categoriesname"},
        ],
        "having_expression": having,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }


def test_distant_is_null_is_rejected():
    where = {"table": "offer", "column": "id", "operator": "IS NULL"}
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(_plan(where, None), SCHEMA, RELATIONSHIPS)
    assert info.value.rule_id == "anti_join_not_adjacent"


def test_having_count_zero_is_accepted():
    having = {
        "aggregate": "COUNT", "table": "offer", "column": "id",
        "operator": "=", "value": 0,
    }
    assert _validate_llm_output(_plan(None, having), SCHEMA, RELATIONSHIPS)


def test_adjacent_is_null_is_accepted():
    plan = _plan({"table": "shop_details", "column": "id", "operator": "IS NULL"}, None)
    plan["joins"] = plan["joins"][:1]
    assert _validate_llm_output(plan, SCHEMA, RELATIONSHIPS)