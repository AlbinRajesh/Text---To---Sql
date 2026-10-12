"""A 'top 1' answer must say so when another row ties with it."""

import asyncio

from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.run_self_fix import run_query
from app.agent.graph import _pick_tie_column


def _run(rows, tie_column="avg_rating"):
    async def fake_fetch(sql, params):
        return rows

    import app.agent.nodes.run_self_fix as module

    original = module.fetch_all_readonly
    module.fetch_all_readonly = fake_fetch
    try:
        return asyncio.run(run_query({
            "sql": "SELECT 1",
            "params": (),
            "columns": ["name", "avg_rating"],
            "row_limit": 1,
            "user_limit": True,
            "tie_column": tie_column,
        }))
    finally:
        module.fetch_all_readonly = original


def test_tie_with_probe_row_is_flagged():
    result = _run([("Clothing", 5.0), ("Restaurent", 5.0)])
    assert result["tie_at_limit"] is True
    assert len(result["results"]) == 1
    answer = _build_result_answer(result, {"group_by": [1]})
    assert "not a unique answer" in answer["message"]


def test_clear_winner_is_not_flagged():
    result = _run([("Clothing", 5.0), ("Restaurent", 4.0)])
    assert result["tie_at_limit"] is False
    answer = _build_result_answer(result, {"group_by": [1]})
    assert "message" not in answer


def test_missing_tie_column_never_flags():
    result = _run([("Clothing", 5.0), ("Restaurent", 5.0)], tie_column=None)
    assert result["tie_at_limit"] is False


def test_rating_tie_notice_survives_primary_key_tiebreak(monkeypatch):
    import app.agent.nodes.run_self_fix as module

    rows = [
        (1, "Shop 1", 5.0, 100),
        (2, "Shop 2", 4.9, 70),
        (3, "Shop 3", 4.8, 50),
        (4, "Shop 4", 4.7, 20),
        (5, "Many reviews", 4.5, 576),
        (6, "Single review", 4.5, 1),
    ]

    async def fake_fetch(sql, params):
        return rows

    monkeypatch.setattr(module, "fetch_all_readonly", fake_fetch)
    query_plan = {
        "select_items": [
            {"column": "id", "alias": "shop_id"},
            {"column": "name", "alias": "shop_name"},
            {"column": "rating", "aggregate": "AVG", "alias": "avg_rating"},
            {"column": "id", "aggregate": "COUNT", "alias": "review_count"},
        ],
        "order_by": [
            {"column": "avg_rating", "direction": "DESC"},
            {"column": "review_count", "direction": "DESC"},
            {"column": "shop_id", "direction": "ASC"},
        ],
    }
    tie_column = _pick_tie_column(
        query_plan, ["shop_id", "shop_name", "avg_rating", "review_count"]
    )
    result = asyncio.run(run_query({
        "sql": "SELECT 1",
        "params": (),
        "columns": ["shop_id", "shop_name", "avg_rating", "review_count"],
        "row_limit": 5,
        "user_limit": True,
        "tie_column": tie_column,
    }))

    assert tie_column == "avg_rating"
    assert result["tie_at_limit"] is True
    answer = _build_result_answer(result, {"group_by": [1]})
    assert answer["tie_at_limit"] is True
    assert "not a unique answer" in answer["message"]


def test_alias_ordering_sets_tie_column():
    query = {
        "select_items": [
            {"column": "name", "alias": "subcategory_name"},
            {"column": "rating", "aggregate": "AVG", "alias": "avg_rating"},
        ],
        "order_by": [{"column": "avg_rating", "direction": "DESC"}],
    }
    assert _pick_tie_column(query, ["subcategory_name", "avg_rating"]) == "avg_rating"


def test_source_column_ordering_does_not_set_tie_column():
    query = {
        "select_items": [{"column": "name", "alias": "shop_name"}],
        "order_by": [{"table": "shop_details", "column": "name", "direction": "ASC"}],
    }
    assert _pick_tie_column(query, ["shop_name"]) is None


from app.agent.graph import _pick_tie_column


def test_alias_ordering_sets_tie_column():
    query = {
        "select_items": [
            {"column": "name", "alias": "subcategory_name"},
            {"column": "rating", "aggregate": "AVG", "alias": "avg_rating"},
        ],
        "order_by": [{"column": "avg_rating", "direction": "DESC"}],
    }
    assert _pick_tie_column(query, ["subcategory_name", "avg_rating"]) == "avg_rating"


def test_source_column_ordering_does_not_set_tie_column():
    query = {
        "select_items": [{"column": "name", "alias": "shop_name"}],
        "order_by": [{"table": "shop_details", "column": "name", "direction": "ASC"}],
    }
    assert _pick_tie_column(query, ["shop_name"]) is None