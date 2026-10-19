"""A 'top 1' answer must say so when another row ties with it."""

import asyncio

from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.run_self_fix import run_query
from app.agent.graph import (
    _pick_tie_column,
    build_tie_expansion_query,
    node_execute,
)


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


def test_tie_expansion_keeps_bounded_frontier():
    import app.agent.nodes.run_self_fix as module

    calls = []

    async def fake_fetch(sql, params):
        calls.append(sql)
        if len(calls) == 1:
            return [("A", 5.0), ("B", 5.0), ("C", 5.0)]
        return [
            ("A", 5.0),
            ("B", 5.0),
            ("C", 5.0),
            ("D", 5.0),
            ("E", 4.0),
        ]

    original = module.fetch_all_readonly
    module.fetch_all_readonly = fake_fetch
    try:
        result = asyncio.run(run_query({
            "sql": "SELECT 1",
            "params": (),
            "columns": ["name", "avg_rating"],
            "row_limit": 2,
            "user_limit": True,
            "tie_column": "avg_rating",
            "tie_expansion_query": {
                "sql": "SELECT expanded",
                "params": (),
                "row_limit": 27,
            },
        }))
    finally:
        module.fetch_all_readonly = original

    assert result["tie_at_limit"] is True
    assert [row["name"] for row in result["results"]] == ["A", "B", "C", "D"]
    assert result["truncated"] is False
    assert len(calls) == 2


def test_tie_expansion_marks_frontier_truncated_at_cap(monkeypatch):
    import app.agent.nodes.run_self_fix as module
    from app.agent.nodes.run_self_fix import MAX_TIE_EXPANSION

    expanded_rows = [
        (f"Shop {index}", 5.0)
        for index in range(1, 4 + MAX_TIE_EXPANSION)
    ]

    async def fake_fetch(sql, params):
        return expanded_rows[:3] if sql == "SELECT initial" else expanded_rows

    monkeypatch.setattr(module, "fetch_all_readonly", fake_fetch)
    result = asyncio.run(run_query({
        "sql": "SELECT initial",
        "params": (),
        "columns": ["name", "avg_rating"],
        "row_limit": 2,
        "user_limit": True,
        "tie_column": "avg_rating",
        "tie_expansion_query": {
            "sql": "SELECT expanded",
            "params": (),
            "row_limit": 2 + MAX_TIE_EXPANSION,
        },
    }))

    assert len(result["results"]) == 2 + MAX_TIE_EXPANSION
    assert result["tie_at_limit"] is True
    assert result["truncated"] is True


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


def test_tie_expansion_plan_is_compiled_with_a_bounded_limit():
    query = {
        "table": "bookings",
        "joins": [],
        "select_items": [
            {
                "table": "bookings",
                "column": "time_slot_month",
                "aggregate": None,
                "alias": "booking_month",
            },
            {
                "table": "bookings",
                "column": "id",
                "aggregate": "COUNT",
                "alias": "booking_count",
            },
        ],
        "where_expression": None,
        "group_by": [{"table": "bookings", "column": "time_slot_month"}],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"column": "booking_count", "direction": "DESC"}],
        "limit": 1,
    }
    schema = {
        "bookings": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "time_slot_month", "data_type": "varchar", "key": ""},
        ],
    }

    compiled = build_tie_expansion_query(query, ["bookings"], schema)

    assert compiled is not None
    assert compiled["user_limit"] is True
    assert compiled["row_limit"] == 26
    assert compiled["params"][-1] == 27


def test_node_execute_passes_local_tables_to_tie_expansion(monkeypatch):
    build_calls = []

    def fake_build_tie_expansion_query(query, valid_tables, schema, relationships):
        build_calls.append((query, valid_tables, schema, relationships))
        return {"sql": "SELECT expanded", "params": [], "row_limit": 26}

    async def fake_run_query(query):
        assert query["tie_expansion_query"]["sql"] == "SELECT expanded"
        return {
            "results": [("May", 8)],
            "columns": ["booking_month", "booking_count"],
            "row_count": 1,
        }

    monkeypatch.setattr(
        "app.agent.graph.build_tie_expansion_query",
        fake_build_tie_expansion_query,
    )
    monkeypatch.setattr("app.agent.graph.run_query", fake_run_query)
    query_plan = {
        "table": "bookings",
        "joins": [],
        "select_items": [
            {
                "table": "bookings",
                "column": "time_slot_month",
                "alias": "booking_month",
            },
            {
                "table": "bookings",
                "column": "id",
                "aggregate": "COUNT",
                "alias": "booking_count",
            },
        ],
        "where_expression": None,
        "group_by": [{"table": "bookings", "column": "time_slot_month"}],
        "order_by": [{"column": "booking_count", "direction": "DESC"}],
        "limit": 1,
    }
    schema = {
        "bookings": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "time_slot_month", "data_type": "varchar", "key": ""},
        ],
    }
    state = {
        "safety_check_result": {
            "sql": "SELECT booking_month, booking_count FROM bookings LIMIT %s",
            "params": [2],
            "table": "bookings",
            "row_limit": 1,
            "user_limit": True,
        },
        "generated_query": query_plan,
        "schema_context": schema,
        "schema_relationships": [],
        "classifier_result": {"rewritten_question": "Which month had the most bookings?"},
    }

    result = asyncio.run(node_execute(state))

    assert result["execution_exhausted"] is False
    assert build_calls[0][1] == ["bookings"]
    assert build_calls[0][2] is schema
    assert build_calls[0][3] == []