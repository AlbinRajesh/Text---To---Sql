import asyncio

from app.agent.nodes import did_you_mean
from app.agent.graph import _column_formats, node_execute
from app.agent.nodes.did_you_mean import build_suggestion_execution_plan
from app.agent.nodes.run_self_fix import run_query


SCHEMA = {
    "offer": [
        {"column": "offer_heading", "data_type": "varchar"},
        {"column": "category", "data_type": "varchar"},
        {"column": "offer_id", "data_type": "bigint"},
        {"column": "email", "data_type": "varchar"},
        {"column": "last_name", "data_type": "varchar"},
        {"column": "pincode", "data_type": "int"},
    ],
    "users": [
        {"column": "display_name", "data_type": "varchar"},
    ],
}


def _filter(column, value, table="offer", operator="="):
    return {
        "table": table,
        "column": column,
        "operator": operator,
        "value": value,
    }


def _query(expression):
    return {
        "table": "offer",
        "joins": [],
        "where_expression": expression,
        "select_items": [{"table": "offer", "column": "offer_heading", "alias": "offer_heading"}],
        "group_by": [],
    }


def test_value_suggestions_find_punctuation_and_typo_variants(monkeypatch):
    executed = []

    async def fake_fetch(sql, params):
        executed.append((sql, params))
        return [("Lucky Users Special Offer!",), ("Another Offer",)]

    monkeypatch.setattr(did_you_mean, "fetch_all_readonly", fake_fetch)
    result = asyncio.run(did_you_mean.find_value_suggestions(
        _query({
            "boolean": "AND",
            "conditions": [
                _filter("offer_heading", "Lucky Users Special Offer"),
                _filter("category", "Dining"),
            ],
        }),
        SCHEMA,
    ))

    assert result == [{
        "table": "offer",
        "column": "offer_heading",
        "original": "Lucky Users Special Offer",
        "suggested": "Lucky Users Special Offer!",
    }]
    assert len(executed) == 2
    sql, params = executed[0]
    assert "`offer`.`offer_heading` AS `_suggestion_value`" in sql
    assert "`offer`.`category` = %s" in sql
    assert "GROUP BY `offer`.`offer_heading`" in sql
    assert "Lucky Users Special Offer" not in sql
    assert params == ("Dining", 1001)
    assert all("Lucky Users Special Offer" not in lookup_sql for lookup_sql, _ in executed)


def test_value_suggestions_skip_unsafe_or_unsupported_filters(monkeypatch):
    executed = []

    async def fake_fetch(sql, params):
        executed.append(sql)
        assert params == ()
        return [("candidate",)]

    monkeypatch.setattr(did_you_mean, "fetch_all_readonly", fake_fetch)
    expressions = [
        {"boolean": "OR", "conditions": [
            _filter("offer_heading", "Lucky Users"),
            _filter("category", "Dining"),
        ]},
        _filter("offer_heading", "Lucky Users", operator="LIKE"),
        _filter("offer_id", "123"),
        _filter("email", "person@example.com"),
        _filter("last_name", "Customer Name"),
        _filter("missing_column", "Lucky Users"),
        _filter("display_name", "Customer Name", table="users"),
    ]

    for expression in expressions:
        result = asyncio.run(
            did_you_mean.find_value_suggestions(_query(expression), SCHEMA)
        )
        assert result == []

    assert executed == []


def test_value_suggestion_lookup_failures_do_not_fail_the_query(monkeypatch):
    async def broken_fetch(sql, params):
        assert "`offer_heading`" in sql
        assert params == (1001,)
        raise RuntimeError("replica unavailable")

    monkeypatch.setattr(did_you_mean, "fetch_all_readonly", broken_fetch)
    result = asyncio.run(did_you_mean.find_value_suggestions(
        _query(_filter("offer_heading", "Lucky Users Special Offer")),
        SCHEMA,
    ))

    assert result == []


def test_suggestion_execution_plan_replaces_one_filter_and_is_safety_checked():
    query = _query({
        "boolean": "AND",
        "conditions": [
            _filter("offer_heading", "Lucky Users Special Offer"),
            _filter("category", "Dining"),
        ],
    })
    compiled = build_suggestion_execution_plan(
        query,
        SCHEMA,
        [],
        {
            "table": "offer",
            "column": "offer_heading",
            "original": "Lucky Users Special Offer",
            "suggested": "Lucky Users Special Offer!",
        },
    )

    assert compiled is not None
    assert compiled["safety_check_result"]["params"] == [
        "Lucky Users Special Offer!",
        "Dining",
        201,
    ]
    assert compiled["safety_check_result"]["sql"].count("%s") == 3
    assert query["where_expression"]["conditions"][0]["value"] == (
        "Lucky Users Special Offer"
    )


def test_suggestion_execution_plan_rejects_ambiguous_duplicate_filter():
    query = _query({
        "boolean": "AND",
        "conditions": [
            _filter("offer_heading", "Lucky Users Special Offer"),
            _filter("offer_heading", "Lucky Users Special Offer"),
        ],
    })
    compiled = build_suggestion_execution_plan(
        query,
        SCHEMA,
        [],
        {
            "table": "offer",
            "column": "offer_heading",
            "original": "Lucky Users Special Offer",
            "suggested": "Lucky Users Special Offer!",
        },
    )

    assert compiled is None


def test_value_suggestions_respect_the_total_lookup_deadline(monkeypatch):
    async def slow_fetch(sql, params):
        assert "MAX_EXECUTION_TIME(4000)" in sql
        assert params == (1001,)
        await asyncio.sleep(0.05)
        return [("Lucky Users Special Offer!",)]

    monkeypatch.setattr(did_you_mean, "LOOKUP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(did_you_mean, "fetch_all_readonly", slow_fetch)
    result = asyncio.run(did_you_mean.find_value_suggestions(
        _query(_filter("offer_heading", "Lucky Users Special Offer")),
        SCHEMA,
    ))

    assert result == []


def test_identifier_columns_are_marked_by_source_or_alias():
    formats = _column_formats(
        {
            "select_items": [
                {"table": "offer", "column": "pincode", "alias": "postal"},
                {"table": "offer", "column": "shop_id", "alias": "shop"},
                {"table": "offer", "column": "amount", "alias": "total_amount"},
                {"table": "offer", "column": "amount", "aggregate": "SUM", "alias": "sum_amount"},
            ]
        },
        SCHEMA,
        "offer",
    )

    assert formats == {"postal": "identifier", "shop": "identifier"}


def test_run_query_serializes_identifier_values_without_losing_digits(monkeypatch):
    async def fake_fetch(sql, params):
        assert "`pincode`" in sql
        assert params == ()
        return [(629201,)]

    monkeypatch.setattr(
        "app.agent.nodes.run_self_fix.fetch_all_readonly",
        fake_fetch,
    )
    result = asyncio.run(run_query({
        "sql": "SELECT `pincode` FROM `offer`",
        "params": (),
        "columns": ["postal"],
        "column_formats": {"postal": "identifier"},
        "row_limit": 10,
    }))

    assert result["results"] == [{"postal": "629201"}]


def test_execute_attaches_suggestions_only_for_nonaggregate_zero_rows(monkeypatch):
    calls = []
    stored = []

    async def fake_run_query(_query):
        assert _query["column_formats"] == {}
        return {"results": [], "columns": ["offer_heading"], "count": 0, "row_count": 0}

    async def fake_suggestions(query, schema, relationships):
        calls.append((query, schema, relationships))
        return [{
            "table": "offer",
            "column": "offer_heading",
            "original": "Lucky Users Special Offer",
            "suggested": "Lucky Users Special Offer!",
        }]

    async def fake_store(conversation_id, plans):
        stored.append((conversation_id, plans))
        return ["suggestion-token"]

    monkeypatch.setattr("app.agent.graph.run_query", fake_run_query)
    monkeypatch.setattr("app.agent.graph.find_value_suggestions", fake_suggestions)
    monkeypatch.setattr("app.agent.graph.store_suggestion_plans", fake_store)
    state = {
        "safety_check_result": {
            "sql": "SELECT `offer_heading` FROM `offer` WHERE `offer_heading` = %s",
            "params": ["Lucky Users Special Offer"],
            "table": "offer",
            "row_limit": 200,
            "user_limit": False,
        },
        "generated_query": _query(_filter("offer_heading", "Lucky Users Special Offer")),
        "schema_context": SCHEMA,
        "schema_relationships": [],
        "conversation_id": "conversation-id",
        "classifier_result": {"rewritten_question": "show an offer"},
    }

    result = asyncio.run(node_execute(state))

    assert len(calls) == 1
    assert result["execution_result"]["suggestions"][0]["suggested"] == (
        "Lucky Users Special Offer!"
    )
    assert result["execution_result"]["column_formats"] == {}
    assert result["execution_result"]["suggestions"][0]["suggestion_id"] == (
        "suggestion-token"
    )
    assert stored[0][0] == "conversation-id"
    assert stored[0][1][0]["validated_query"]["params"] == (
        "Lucky Users Special Offer!",
        201,
    )


def test_execute_does_not_look_up_suggestions_for_aggregate_zero_rows(monkeypatch):
    async def fake_run_query(_query):
        assert _query["columns"] == ["offer_count"]
        return {
            "results": [],
            "columns": ["offer_count"],
            "count": 0,
            "row_count": 0,
            "no_data_reason": "no_rows",
        }

    async def unexpected_suggestions(*args):
        assert len(args) == 3
        raise AssertionError("Aggregate query should not look up value suggestions")

    monkeypatch.setattr("app.agent.graph.run_query", fake_run_query)
    monkeypatch.setattr("app.agent.graph.find_value_suggestions", unexpected_suggestions)
    state = {
        "safety_check_result": {
            "sql": "SELECT COUNT(*) FROM `offer`",
            "params": [],
            "table": "offer",
            "row_limit": 200,
            "user_limit": False,
        },
        "generated_query": {
            **_query(_filter("offer_heading", "Lucky Users")),
            "select_items": [{
                "table": "offer",
                "column": "*",
                "aggregate": "COUNT",
                "alias": "offer_count",
            }],
        },
        "schema_context": SCHEMA,
        "classifier_result": {"rewritten_question": "count offers"},
    }

    result = asyncio.run(node_execute(state))
    assert result["execution_result"]["suggestions"] == []
