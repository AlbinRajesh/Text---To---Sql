import asyncio

from app.agent import graph
from app.services import conversation_context


def test_graph_stores_valid_zero_row_query_context(monkeypatch):
    conversation_context.clear_all_conversation_contexts()

    async def fake_build_and_save_answer(_state):
        return {"final_answer": {}, "saved_to_cache": False}

    monkeypatch.setattr(graph, "build_and_save_answer", fake_build_and_save_answer)
    state = {
        "conversation_id": "conversation-empty-result",
        "user_question": "Show the top five booking counts",
        "classifier_result": {
            "original_question": "Show the top five booking counts",
        },
        "result_grader_verdict": {"is_valid": True},
        "execution_result": {"columns": ["booking_count"], "results": []},
        "generated_query": {
            "table": "bookings",
            "select_items": [{
                "table": "bookings",
                "column": "id",
                "aggregate": "COUNT",
                "alias": "booking_count",
            }],
            "ranking_intent": "top_n",
            "order_by": [{"column": "booking_count", "direction": "DESC"}],
            "limit": 5,
        },
    }

    asyncio.run(graph.node_return_save(state))

    context = conversation_context.get_conversation_context(
        "conversation-empty-result"
    )
    assert len(context) == 1
    assert context[0]["rows"] == []
    assert context[0]["metrics"][0]["aggregate"] == "COUNT"
    assert context[0]["query"]["ranking_intent"] == "top_n"
    assert context[0]["query"]["order_by"] == [
        {"column": "booking_count", "direction": "DESC"}
    ]
    assert context[0]["query"]["limit"] == 5
    assert context[0]["original_question"] == "Show the top five booking counts"


def test_graph_does_not_store_unverified_result_context(monkeypatch):
    conversation_context.clear_all_conversation_contexts()

    async def fake_build_and_save_answer(_state):
        return {"final_answer": {}, "saved_to_cache": False}

    monkeypatch.setattr(graph, "build_and_save_answer", fake_build_and_save_answer)
    state = {
        "conversation_id": "conversation-unverified",
        "classifier_result": {"original_question": "Count bookings"},
        "result_grader_verdict": {"is_valid": False},
        "execution_result": {"columns": ["booking_count"], "results": []},
        "generated_query": {"table": "bookings"},
    }

    asyncio.run(graph.node_return_save(state))

    assert conversation_context.get_conversation_context(
        "conversation-unverified"
    ) == []


def test_conversation_context_is_bounded_and_isolated():
    conversation_context.clear_all_conversation_contexts()
    conversation_context.store_verified_turn(
        "conversation-a",
        columns=["category", "booking_count"],
        rows=[{"category": "Services", "booking_count": 8}],
        metrics=[{"aggregate": "COUNT", "alias": "booking_count"}],
        filters={"table": "bookings"},
    )

    context = conversation_context.get_conversation_context("conversation-a")
    assert context[0]["rows"] == [{"category": "Services", "booking_count": 8}]
    assert conversation_context.get_conversation_context("conversation-b") == []

    context[0]["rows"][0]["category"] = "mutated"
    assert (
        conversation_context.get_conversation_context("conversation-a")[0]["rows"][0]["category"]
        == "Services"
    )


def test_conversation_context_stores_query_context_for_empty_results():
    conversation_context.clear_all_conversation_contexts()
    conversation_context.store_verified_turn(
        "conversation-a",
        columns=["booking_count"],
        rows=[],
        metrics=[{
            "table": "bookings",
            "column": "id",
            "aggregate": "COUNT",
            "alias": "booking_count",
        }],
        filters=None,
        query={
            "ranking_intent": "top_n",
            "order_by": [{"column": "booking_count", "direction": "DESC"}],
            "limit": 5,
        },
        original_question="Show the top five booking counts",
    )
    context = conversation_context.get_conversation_context("conversation-a")
    assert context[0]["rows"] == []
    assert context[0]["metrics"][0]["aggregate"] == "COUNT"
    assert context[0]["query"] == {
        "ranking_intent": "top_n",
        "order_by": [{"column": "booking_count", "direction": "DESC"}],
        "limit": 5,
    }
    assert context[0]["original_question"] == "Show the top five booking counts"
