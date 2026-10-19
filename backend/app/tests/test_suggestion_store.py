import asyncio
import json

import pytest

from app.services import suggestion_store


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    async def ping(self):
        return True

    async def set(self, key, value, ex):
        self.values[key] = value
        self.expiry[key] = ex

    async def eval(self, script, key_count, key, conversation_id, token):
        assert key_count == 1
        encoded = self.values.get(key)
        if encoded is None:
            return [0]
        bundle = json.loads(encoded)
        if bundle["conversation_id"] != conversation_id:
            return [-1]
        plan = bundle["plans"].get(token)
        if plan is None:
            return [-1]
        del self.values[key]
        return [1, json.dumps(plan)]

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)


def test_store_defaults_to_local_redis_when_url_is_not_set(monkeypatch):
    redis = FakeRedis()
    used_urls = []

    def fake_from_url(url, **kwargs):
        used_urls.append(url)
        return redis

    monkeypatch.delenv("SUGGESTION_REDIS_URL", raising=False)
    monkeypatch.setattr(suggestion_store.Redis, "from_url", fake_from_url)

    asyncio.run(suggestion_store.initialize_suggestion_store())

    assert used_urls == [suggestion_store.DEFAULT_SUGGESTION_REDIS_URL]
    assert asyncio.run(suggestion_store.suggestion_fast_path_available())

def test_fast_path_availability_tracks_configured_redis(monkeypatch):
    monkeypatch.setattr(suggestion_store, "_redis", None)
    assert not asyncio.run(suggestion_store.suggestion_fast_path_available())

    monkeypatch.setattr(suggestion_store, "_redis", FakeRedis())
    assert asyncio.run(suggestion_store.suggestion_fast_path_available())


def test_stores_three_plans_with_expiration_and_consumes_only_once(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(suggestion_store, "_redis", redis)
    plans = [{"value": f"choice-{index}"} for index in range(3)]

    tokens = asyncio.run(
        suggestion_store.store_suggestion_plans("conversation-a", plans)
    )

    assert len(tokens) == 3
    assert all(len(token) >= 32 for token in tokens)
    key = suggestion_store._redis_key("conversation-a")
    assert redis.expiry[key] == suggestion_store.SUGGESTION_TTL_SECONDS

    selected = asyncio.run(
        suggestion_store.consume_suggestion_plan("conversation-a", tokens[1])
    )
    assert selected == plans[1]
    with pytest.raises(suggestion_store.SuggestionNotFoundError):
        asyncio.run(
            suggestion_store.consume_suggestion_plan("conversation-a", tokens[0])
        )


def test_suggestion_token_is_bound_to_conversation(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(suggestion_store, "_redis", redis)
    token = asyncio.run(
        suggestion_store.store_suggestion_plans(
            "conversation-a",
            [{"value": "safe"}],
        )
    )[0]

    with pytest.raises(suggestion_store.SuggestionNotFoundError):
        asyncio.run(
            suggestion_store.consume_suggestion_plan("conversation-b", token)
        )
    assert suggestion_store._redis_key("conversation-a") in redis.values


def test_regular_question_clears_all_pending_suggestions(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(suggestion_store, "_redis", redis)
    asyncio.run(
        suggestion_store.store_suggestion_plans(
            "conversation-a",
            [{"value": "safe"}],
        )
    )

    asyncio.run(suggestion_store.clear_suggestion_plans("conversation-a"))

    assert suggestion_store._redis_key("conversation-a") not in redis.values


def test_suggestion_store_fails_closed_when_redis_is_not_configured(monkeypatch):
    monkeypatch.setattr(suggestion_store, "_redis", None)

    with pytest.raises(suggestion_store.SuggestionStoreUnavailableError):
        asyncio.run(
            suggestion_store.store_suggestion_plans(
                "conversation-a",
                [{"value": "safe"}],
            )
        )
