"""A repeated category/subcategory name must be refused, not guessed."""

import pytest

from app.agent.nodes.generate_query import (
    UnanswerableQuestionError,
    _validate_llm_output,
)

SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
        {"column": "subcategory_id", "data_type": "bigint", "key": ""},
    ],
    "subcategories": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "subcategoryname", "data_type": "varchar", "key": ""},
    ],
}
RELATIONSHIPS = [{
    "constraint_name": "reviewed_shop_details_subcategory_id",
    "table": "shop_details",
    "referenced_table": "subcategories",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [{"column": "subcategory_id", "referenced_column": "id"}],
}]


@pytest.fixture(autouse=True)
def repeated_names(monkeypatch):
    monkeypatch.setattr(
        "app.agent.nodes.generate_query.get_ambiguous_names",
        lambda: {"subcategories.subcategoryname": ["all", "sports"]},
    )


def _plan(where):
    return {
        "answerable": True,
        "reasoning": "t",
        "table": "shop_details",
        "joins": [{
            "table": "subcategories",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_shop_details_subcategory_id",
                "table": "shop_details",
                "referenced_table": "subcategories",
            },
        }],
        "select_items": [{
            "table": "shop_details", "column": "name",
            "aggregate": None, "distinct": False, "alias": "shop_name",
        }],
        "where_expression": where,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }


def _filter(operator, value):
    return {
        "table": "subcategories", "column": "subcategoryname",
        "operator": operator, "value": value,
    }


def test_repeated_name_is_refused():
    with pytest.raises(UnanswerableQuestionError, match="more than one"):
        _validate_llm_output(_plan(_filter("=", "Sports")), SCHEMA, RELATIONSHIPS)


def test_repeated_name_is_refused_case_insensitively():
    with pytest.raises(UnanswerableQuestionError):
        _validate_llm_output(_plan(_filter("=", "sPoRtS")), SCHEMA, RELATIONSHIPS)


def test_repeated_name_inside_in_list_is_refused():
    with pytest.raises(UnanswerableQuestionError):
        _validate_llm_output(
            _plan(_filter("IN", ["Restaurent", "All"])), SCHEMA, RELATIONSHIPS
        )


def test_unique_name_is_accepted():
    assert _validate_llm_output(
        _plan(_filter("=", "Restaurent")), SCHEMA, RELATIONSHIPS
    )