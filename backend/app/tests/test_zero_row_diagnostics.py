import asyncio

from app.agent import graph
from app.agent.nodes.return_save import _build_result_answer


SCHEMA = {
    "shop_details": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
        {"column": "status"},
    ],
}


def _query(where_expression=None, joins=None):
    return {
        "table": "shop_details",
        "joins": joins or [],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": where_expression,
        "group_by": [],
        "order_by": [],
        "ranking_intent": "none",
    }


def _stub_compiler(monkeypatch, captured):
    def safety_check(query, **kwargs):
        captured.append(query)
        return {"sql": "probe", "params": [], "row_limit": 1, "user_limit": True}

    def build_execution_query(query, safety_result, *args):
        return {
            "sql": safety_result["sql"],
            "params": (),
            "columns": ["shop_name"],
            "row_limit": 1,
            "user_limit": True,
        }, ["shop_name"]

    monkeypatch.setattr(graph, "run_safety_check", safety_check)
    monkeypatch.setattr(
        graph,
        "_build_validated_execution_query",
        build_execution_query,
    )


def test_zero_row_diagnostic_identifies_where_filter_without_mutating_plan(monkeypatch):
    captured = []
    _stub_compiler(monkeypatch, captured)

    async def fake_run_query(validated_query):
        return {
            "results": [{"shop_name": "Example"}],
            "row_count": 1,
        }

    monkeypatch.setattr(graph, "run_query", fake_run_query)
    query = _query({
        "table": "shop_details",
        "column": "status",
        "operator": "=",
        "value": 0,
    })

    diagnostic = asyncio.run(
        graph._diagnose_zero_rows(query, SCHEMA, [])
    )

    assert diagnostic is not None
    assert "`shop_details.status = 0`" in diagnostic["message"]
    assert query["where_expression"]["value"] == 0
    assert len(captured) == 1
    assert captured[0]["where_expression"] is None


def test_zero_row_diagnostic_checks_join_filters(monkeypatch):
    captured = []
    _stub_compiler(monkeypatch, captured)

    async def fake_run_query(validated_query):
        return {
            "results": [{"shop_name": "Example"}],
            "row_count": 1,
        }

    monkeypatch.setattr(graph, "run_query", fake_run_query)
    query = _query(joins=[{
        "table": "shop_address",
        "conditions": [{
            "table": "shop_address",
            "column": "city",
            "operator": "=",
            "value": "Unknown",
        }],
    }])

    diagnostic = asyncio.run(
        graph._diagnose_zero_rows(query, SCHEMA, [])
    )

    assert diagnostic is not None
    assert "`shop_address.city = 'Unknown'`" in diagnostic["message"]
    assert captured[0]["joins"][0]["conditions"] == []
    assert query["joins"][0]["conditions"]


def test_zero_row_diagnostic_caps_additional_read_probes(monkeypatch):
    captured = []
    _stub_compiler(monkeypatch, captured)

    async def fake_run_query(validated_query):
        return {"results": [], "row_count": 0}

    monkeypatch.setattr(graph, "run_query", fake_run_query)
    query = _query({
        "boolean": "AND",
        "conditions": [
            {
                "table": "shop_details",
                "column": "status",
                "operator": "=",
                "value": value,
            }
            for value in range(6)
        ],
    })

    diagnostic = asyncio.run(
        graph._diagnose_zero_rows(query, SCHEMA, [])
    )

    assert diagnostic is None
    assert len(captured) == 4


def test_zero_row_diagnostic_is_included_in_empty_answer():
    diagnostic = {
        "message": "Relaxing `shop_details.status = 0` would return rows.",
        "relaxed_filters": ["shop_details.status = 0"],
    }

    answer = _build_result_answer(
        {
            "results": [],
            "columns": ["shop_name"],
            "row_count": 0,
            "zero_row_diagnostic": diagnostic,
        },
        {},
    )

    assert answer["message"] == diagnostic["message"]
    assert answer["zero_row_diagnostic"] == diagnostic
