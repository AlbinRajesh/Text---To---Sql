import json
import logging
import asyncio

import pytest

from app.agent import graph
from app.agent.nodes import safety_check
from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check


def test_sql_build_rejection_logs_one_redacted_structured_plan(caplog):
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "name", "data_type": "text", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ]
    }
    query = {
        "table": "shop_details",
        "joins": [],
        "select_items": [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "alias": "shop_name",
            },
            {
                "table": "shop_details",
                "column": "id",
                "aggregate": "COUNT",
                "alias": "shop_count",
            },
        ],
        "where_expression": {
            "table": "shop_details",
            "column": "name",
            "operator": "=",
            "value": "private-user-literal",
        },
        "group_by": [{"table": "shop_details", "column": "name"}],
        "having_expression": None,
        "order_by": [{"table": "shop_details", "column": "status", "direction": "ASC"}],
    }

    with caplog.at_level(logging.ERROR, logger="app.agent.nodes.safety_check"):
        with pytest.raises(
            SafetyViolationError,
            match="ORDER BY in a grouped or aggregate query",
        ):
            run_safety_check(query, ["shop_details"], schema, [])

    records = [
        record for record in caplog.records
        if getattr(record, "event", None) == "safety_check_rejected_plan"
    ]
    assert len(records) == 1
    serialized = records[0].getMessage().split(
        "Safety check rejected query plan: ", 1
    )[1]
    logged = json.loads(serialized)
    assert "ORDER BY in a grouped or aggregate query" in logged["reason"]
    assert logged["plan"]["table"] == "shop_details"
    assert logged["plan"]["where_expression"]["value"] == "<redacted>"
    assert "private-user-literal" not in serialized


def test_rejected_plan_log_stays_within_size_limit():
    from app.agent.nodes.safety_check import (
        _MAX_REJECTED_PLAN_LOG_CHARS,
        _rejected_plan_log_json,
    )

    encoded = _rejected_plan_log_json(
        {
            "table": "shop_details",
            "joins": [
                {"table": f"table_{index}" * 200, "join_type": "LEFT"}
                for index in range(100)
            ],
            "select_items": [
                {"table": "shop_details", "column": f"column_{index}" * 200}
                for index in range(100)
            ],
        },
        "x" * 1000,
    )
    assert len(encoded) <= _MAX_REJECTED_PLAN_LOG_CHARS
    assert json.loads(encoded)["truncated"] is True


def test_graph_repair_rejection_emits_a_record_for_each_safety_attempt(
    monkeypatch, caplog
):
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "name", "data_type": "text", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ]
    }
    query = {
        "table": "shop_details",
        "joins": [],
        "select_items": [
            {"table": "shop_details", "column": "id", "aggregate": "COUNT",
             "alias": "shop_count"},
        ],
        "where_expression": None,
        "group_by": [{"table": "shop_details", "column": "name"}],
        "having_expression": None,
        "order_by": [{"table": "shop_details", "column": "status", "direction": "ASC"}],
    }
    state = {
        "generated_query": query,
        "schema_context": schema,
        "schema_relationships": [],
        "classifier_result": {
            "original_question": "count shops and order by status",
            "rewritten_question": "count shops and order by status",
        },
    }

    async def return_same_plan(*args, **kwargs):
        return query

    monkeypatch.setattr(
        graph, "repair_query_after_safety_rejection", return_same_plan
    )
    with caplog.at_level(logging.ERROR):
        asyncio.run(graph.node_safety_check(state))

    records = [
        record for record in caplog.records
        if getattr(record, "event", None) == "safety_check_rejected_plan"
    ]
    assert len(records) == 2
    assert state["escalated"] is True
    assert state["safety_repair_attempts"] == 1
    assert state["escalation_info"]["attempts_made"] == 2
    assert not any("Node 7 rejected query" in record.getMessage() for record in caplog.records)


def test_ast_rejection_is_logged_when_graph_handles_safety_exception(monkeypatch, caplog):
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "name", "data_type": "text", "key": ""},
        ]
    }
    query = {
        "table": "shop_details",
        "joins": [],
        "select_items": [
            {"table": "shop_details", "column": "name", "aggregate": None},
        ],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "order_by": [],
    }

    def reject_ast(*args, **kwargs):
        raise SafetyViolationError("AST rejected query")

    monkeypatch.setattr(safety_check, "_ast_verify", reject_ast)
    state = {
        "generated_query": query,
        "schema_context": schema,
        "schema_relationships": [],
    }

    with caplog.at_level(logging.ERROR):
        asyncio.run(graph.node_safety_check(state))

    records = [
        record for record in caplog.records
        if getattr(record, "event", None) == "safety_check_rejected_plan"
    ]
    assert len(records) == 1
    assert '"reason":"AST rejected query"' in records[0].getMessage()
    assert state["safety_violation_reason"] == "AST rejected query"
    assert state["escalated"] is True
