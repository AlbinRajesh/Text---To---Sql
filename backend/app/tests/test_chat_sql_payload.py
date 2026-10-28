import asyncio

from app.api.chat import _build_response
from app.services.timing import set_emitter, timed_node


def test_build_response_includes_sql_for_executed_query():
    sql = "SELECT name FROM shops"
    response = _build_response(
        {
            "final_answer": {"results": [], "columns": []},
            "safety_check_result": {"sql": sql},
            "execution_result": {},
        },
        "request-1",
        "conversation-1",
    )

    assert response.sql == sql


def test_build_response_does_not_include_unexecuted_sql():
    response = _build_response(
        {
            "final_answer": {"results": [], "columns": []},
            "safety_check_result": {"sql": "SELECT name FROM shops"},
        },
        "request-2",
        "conversation-2",
    )

    assert response.sql is None


def test_safety_check_streams_validated_sql():
    events = []

    async def safety_check(state):
        return {
            **state,
            "safety_check_result": {"sql": "SELECT name FROM shops"},
            "escalated": False,
        }

    async def run_node():
        set_emitter(events.append)
        await timed_node("safety_check", safety_check)({})

    asyncio.run(run_node())

    assert {"type": "sql", "sql": "SELECT name FROM shops"} in events
