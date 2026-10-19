import asyncio
from uuid import uuid4

from app.api import chat
from app import main
from app.services.suggestion_store import (
    SuggestionNotFoundError,
    SuggestionStoreUnavailableError,
)


def _request(conversation_id):
    return chat.SuggestionRedemptionRequest(
        conversation_id=conversation_id,
        suggestion_id="x" * 43,
    )


def _plan():
    return {
        "validated_query": {
            "sql": "SELECT `offer_heading` FROM `offer` WHERE `offer_heading` = %s",
            "params": ("Lucky Users Special Offer!",),
            "columns": ["offer_heading"],
            "row_limit": 200,
            "user_limit": False,
            "column_formats": {},
        },
        "generated_query": {
            "group_by": [],
            "select_items": [{
                "table": "offer",
                "column": "offer_heading",
                "alias": "offer_heading",
            }],
        },
    }


def test_redemption_executes_plan_without_running_pipeline(monkeypatch):
    conversation_id = uuid4()
    executed = []

    async def fake_consume(conversation, token):
        assert conversation == str(conversation_id)
        assert token == "x" * 43
        return _plan()

    async def fake_run_query(query):
        executed.append(query)
        return {
            "results": [{"offer_heading": "Lucky Users Special Offer!"}],
            "columns": ["offer_heading"],
            "count": 1,
            "row_count": 1,
            "row_limit": 200,
            "truncated": False,
        }

    async def unexpected_pipeline(*args, **kwargs):
        raise AssertionError("Fast redemption must not invoke the LLM graph.")

    monkeypatch.setattr(chat, "consume_suggestion_plan", fake_consume)
    monkeypatch.setattr(chat, "run_query", fake_run_query)
    monkeypatch.setattr(chat, "run_pipeline", unexpected_pipeline)

    response = asyncio.run(chat.redeem_suggestion(_request(conversation_id)))

    assert response.status == "ok"
    assert response.fast_path is True
    assert response.conversation_id == str(conversation_id)
    assert response.results == [{"offer_heading": "Lucky Users Special Offer!"}]
    assert len(executed) == 1


def test_invalid_or_replayed_suggestion_returns_gone_without_query(monkeypatch):
    async def missing_plan(*args):
        raise SuggestionNotFoundError("expired")

    async def unexpected_query(*args):
        raise AssertionError("Invalid suggestion must never execute SQL.")

    monkeypatch.setattr(chat, "consume_suggestion_plan", missing_plan)
    monkeypatch.setattr(chat, "run_query", unexpected_query)

    response = asyncio.run(chat.redeem_suggestion(_request(uuid4())))

    assert response.status_code == 410
    assert "expired" in response.body.decode()


def test_unavailable_suggestion_store_returns_service_unavailable(monkeypatch):
    async def unavailable(*args):
        raise SuggestionStoreUnavailableError("Redis unavailable")

    monkeypatch.setattr(chat, "consume_suggestion_plan", unavailable)

    response = asyncio.run(chat.redeem_suggestion(_request(uuid4())))

    assert response.status_code == 503


def test_health_reports_suggestion_fast_path_status(monkeypatch):
    async def healthy_database():
        return True

    monkeypatch.setattr(main, "health_check", healthy_database)
    async def unavailable_fast_path():
        return False

    monkeypatch.setattr(
        main,
        "suggestion_fast_path_available",
        unavailable_fast_path,
    )
    unavailable = asyncio.run(main.check_health())
    assert unavailable["database"] == "connected"
    assert unavailable["suggestion_fast_path"] == "unavailable"

    async def available_fast_path():
        return True

    monkeypatch.setattr(
        main,
        "suggestion_fast_path_available",
        available_fast_path,
    )
    available = asyncio.run(main.check_health())
    assert available["suggestion_fast_path"] == "available"
