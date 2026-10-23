"""Single-query tests for bounded single-best tie results."""

import asyncio

from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.run_self_fix import run_query


def test_single_best_returns_every_tie_from_one_query(monkeypatch):
    import app.agent.nodes.run_self_fix as module

    calls = []

    async def fake_fetch(sql, params):
        calls.append((sql, params))
        return [("Shop A", 5.0), ("Shop B", 5.0)]

    monkeypatch.setattr(module, "fetch_all_readonly", fake_fetch)
    result = asyncio.run(run_query({
        "sql": "SELECT ranked_ties",
        "params": (),
        "columns": ["shop_name", "average_rating"],
        "row_limit": 1000,
        "user_limit": False,
        "single_best_with_ties": True,
    }))

    assert len(calls) == 1
    assert [row["shop_name"] for row in result["results"]] == ["Shop A", "Shop B"]
    assert result["tie_at_limit"] is True
    answer = _build_result_answer(result, {"group_by": [1]})
    assert "not a unique answer" in answer["message"]


def test_single_best_with_one_winner_has_no_tie_notice(monkeypatch):
    import app.agent.nodes.run_self_fix as module

    async def fake_fetch(sql, params):
        return [("Shop A", 5.0)]

    monkeypatch.setattr(module, "fetch_all_readonly", fake_fetch)
    result = asyncio.run(run_query({
        "sql": "SELECT ranked_ties",
        "params": (),
        "columns": ["shop_name", "average_rating"],
        "row_limit": 1000,
        "user_limit": False,
        "single_best_with_ties": True,
    }))

    assert result["tie_at_limit"] is False
    assert "message" not in _build_result_answer(result, {"group_by": [1]})


def test_single_best_tie_result_cap_marks_omitted_rows(monkeypatch):
    import app.agent.nodes.run_self_fix as module

    async def fake_fetch(sql, params):
        return [(f"Shop {index}", 5.0) for index in range(1001)]

    monkeypatch.setattr(module, "fetch_all_readonly", fake_fetch)
    result = asyncio.run(run_query({
        "sql": "SELECT ranked_ties",
        "params": (),
        "columns": ["shop_name", "average_rating"],
        "row_limit": 1000,
        "user_limit": False,
        "single_best_with_ties": True,
    }))

    assert len(result["results"]) == 1000
    assert result["tie_at_limit"] is True
    assert result["truncated"] is True
    answer = _build_result_answer(result, {"group_by": [1]})
    assert "not all tied rows are shown" in answer["message"]


def test_non_ranked_queries_keep_normal_result_truncation(monkeypatch):
    import app.agent.nodes.run_self_fix as module

    async def fake_fetch(sql, params):
        return [("A",), ("B",), ("C",)]

    monkeypatch.setattr(module, "fetch_all_readonly", fake_fetch)
    result = asyncio.run(run_query({
        "sql": "SELECT rows",
        "params": (),
        "columns": ["name"],
        "row_limit": 2,
        "user_limit": False,
    }))

    assert [row["name"] for row in result["results"]] == ["A", "B"]
    assert result["truncated"] is True
    assert result["tie_at_limit"] is False
