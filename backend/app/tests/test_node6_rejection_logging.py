"""Phase 0: Node 6 rejection diagnostics are structural and never leak values."""

import logging

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output

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
LOGGER = "app.agent.nodes.generate_query"


def _plan(base, joined, select_item):
    return {
        "answerable": True,
        "reasoning": "test",
        "table": base,
        "joins": [{"table": joined, "join_type": "INNER", "relationship": REL}],
        "select_items": [select_item],
        "where_expression": {
            "table": "shop_details", "column": "name",
            "operator": "=", "value": "SECRET_VALUE",
        },
        "group_by": [],
        "count_entity": None,
        "order_by": [],
    }


def _run(plan):
    with pytest.raises(QueryGenerationError):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [], attempt_number=1)


def test_aggregate_rejection_logs_both_predicates(caplog):
    plan = _plan("shop_details", "reviews", {
        "table": "reviews", "column": "rating", "aggregate": "AVG", "alias": "avg_rating",
    })
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _run(plan)
    assert "aggregate_on_base_table=False" in caplog.text
    assert "all_joins_many_to_one=False" in caplog.text
    assert "attempt=1" in caplog.text


def test_count_primary_key_rejection_logs_rule_id(caplog):
    plan = _plan("reviews", "shop_details", {
        "table": "reviews", "column": "shop_id", "aggregate": "COUNT",
        "distinct": True, "alias": "n",
    })
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _run(plan)
    assert "rule_id=joined_count_not_primary_key" in caplog.text
    assert "count_column='shop_id'" in caplog.text


def test_count_not_distinct_rejection_logs_rule_id(caplog):
    plan = _plan("reviews", "shop_details", {
        "table": "reviews", "column": "id", "aggregate": "COUNT",
        "distinct": False, "alias": "n",
    })
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _run(plan)
    assert "rule_id=joined_count_not_distinct" in caplog.text


def test_filter_values_never_appear_in_logs(caplog):
    plan = _plan("reviews", "shop_details", {
        "table": "reviews", "column": "shop_id", "aggregate": "COUNT",
        "distinct": True, "alias": "n",
    })
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _run(plan)
    assert "SECRET_VALUE" not in caplog.text


def test_malformed_join_data_does_not_break_the_rejection(caplog, monkeypatch):
    from app.agent.nodes import generate_query as gq

    def boom(*args, **kwargs):
        raise RuntimeError("logging broke")

    monkeypatch.setattr(gq.logger, "warning", boom)
    plan = _plan("reviews", "shop_details", {
        "table": "reviews", "column": "shop_id", "aggregate": "COUNT",
        "distinct": True, "alias": "n",
    })
    _run(plan)  # still the normal QueryGenerationError, not RuntimeError