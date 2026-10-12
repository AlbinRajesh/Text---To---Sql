import asyncio
import logging

import pytest

from app.agent import graph
from app.agent.nodes import blocked_tables as bt
from app.agent.nodes import schema_grounding, safety_check


SCHEMA = {
    "offer": [{"column": "id"}],
    "products": [{"column": "id"}],
    "shop_details": [{"column": "id"}],
}


def test_blocked_table_is_removed_from_schema():
    result = bt.filter_blocked_schema(SCHEMA)
    assert "products" not in result
    assert {"offer", "shop_details"} <= set(result)


def test_relationships_touching_blocked_tables_are_removed():
    rels = [
        {"table": "offer", "referenced_table": "products"},
        {"table": "offer", "referenced_table": "shop_details"},
    ]
    result = bt.filter_blocked_relationships(rels)
    assert result == [rels[1]]


def test_find_blocked_tables_ignores_case():
    assert bt.find_blocked_tables(["Offer", "PRODUCTS"]) == ["PRODUCTS"]
    assert bt.find_blocked_tables(["offer", "shop_details"]) == []


def test_validate_warns_when_blocked_table_not_in_schema(caplog):
    caplog.set_level(logging.WARNING, logger=bt.__name__)
    bt.validate_blocked_tables({"offer": []})
    assert "not in the database schema" in caplog.text


def test_validate_rejects_a_table_that_is_also_searched(monkeypatch):
    monkeypatch.setattr(bt, "BLOCKED_TABLES", {"offer": "test"})
    with pytest.raises(ValueError):
        bt.validate_blocked_tables(SCHEMA)


def test_refresh_schema_cache_filters_tables_and_relationships(monkeypatch):
    raw_schema = {
        "offer": [{"column": "id"}],
        "products": [{"column": "id"}],
        "shop_details": [{"column": "id"}],
    }
    raw_relationships = [
        {"table": "offer", "referenced_table": "products"},
        {"table": "offer", "referenced_table": "shop_details"},
    ]
    validated_relationships = [
        *raw_relationships,
        {"table": "products", "referenced_table": "shop_details"},
    ]
    validated_inputs = {}
    validate_inputs = []

    async def fetch_schema():
        return raw_schema

    async def fetch_relationships():
        return raw_relationships

    async def fetch_names(tables):
        return {}

    def validate(tables):
        validate_inputs.append(tables)

    def validate_relationships(tables, relationships):
        validated_inputs["tables"] = tables
        validated_inputs["relationships"] = relationships
        return validated_relationships

    monkeypatch.setattr(schema_grounding, "fetch_schema_from_information_schema", fetch_schema)
    monkeypatch.setattr(
        schema_grounding, "fetch_relationships_from_information_schema", fetch_relationships
    )
    monkeypatch.setattr(schema_grounding, "fetch_ambiguous_names", fetch_names)
    monkeypatch.setattr(schema_grounding, "validate_blocked_tables", validate)
    monkeypatch.setattr(schema_grounding, "_validated_relationships", validate_relationships)
    monkeypatch.setattr(schema_grounding, "validate_searchable_columns", lambda *_: None)

    asyncio.run(schema_grounding.refresh_schema_cache())

    assert validate_inputs == [raw_schema]
    assert "products" not in validated_inputs["tables"]
    assert all(
        "products" not in {rel["table"], rel["referenced_table"]}
        for rel in validated_inputs["relationships"]
    )
    assert "products" not in schema_grounding.get_cached_schema()
    assert all(
        "products" not in {rel["table"], rel["referenced_table"]}
        for rel in schema_grounding.get_cached_relationships()
    )


def test_safety_check_blocks_plan_and_ast_table_usage():
    schema = {"products": [{"column": "id"}]}
    query = {
        "table": "products",
        "select_items": [{"table": "products", "column": "id", "alias": None}],
    }

    with pytest.raises(safety_check.SafetyViolationError) as plan_error:
        safety_check.run_safety_check(
            query,
            valid_tables=["products"],
            schema_context=schema,
        )

    assert plan_error.value.rule_id == "blocked_table"
    assert "products" in str(plan_error.value)

    with pytest.raises(safety_check.SafetyViolationError) as ast_error:
        safety_check._ast_verify("SELECT `products`.`id` FROM `products`", schema)

    assert ast_error.value.rule_id == "blocked_table"


def test_blocked_table_safety_violation_does_not_retry(monkeypatch):
    async def unexpected_repair(*args, **kwargs):
        raise AssertionError("blocked-table safety violations must not be retried")

    monkeypatch.setattr(graph, "repair_query_after_safety_rejection", unexpected_repair)
    state = {
        "generated_query": {
            "table": "products",
            "select_items": [{
                "table": "products",
                "column": "id",
                "aggregate": None,
                "alias": "id",
            }],
        },
        "schema_context": {"products": [{"column": "id"}]},
        "schema_relationships": [],
        "classifier_result": {
            "original_question": "show products",
            "rewritten_question": "show products",
        },
        "escalated": False,
    }

    result = asyncio.run(graph.node_safety_check(state))

    assert result["escalated"] is True
    assert result["safety_repair_attempts"] == 0
    assert "not allowed" in result["safety_violation_reason"]