import asyncio
import json
import logging

import pytest
from redis.exceptions import RedisError

from app.api import chat
from app.services import pending_interactions as pending_store


class FakeRedis:
    def __init__(self, *, unavailable=False):
        self.values = {}
        self.expiry = {}
        self.clock = 100.0
        self.unavailable = unavailable

    def _check(self):
        if self.unavailable:
            raise RedisError("simulated redis outage")

    async def ping(self):
        self._check()
        return True

    async def set(self, key, value, ex):
        self._check()
        self.values[key] = value
        self.expiry[key] = self.clock + ex

    async def get(self, key):
        self._check()
        if self.expiry.get(key, 0) <= self.clock:
            self.values.pop(key, None)
            self.expiry.pop(key, None)
            return None
        return self.values.get(key)

    async def delete(self, key):
        self._check()
        self.expiry.pop(key, None)
        return int(self.values.pop(key, None) is not None)

    async def eval(self, script, key_count, key, *args):
        self._check()
        assert key_count == 1
        encoded = await self.get(key)
        if encoded is None:
            return None if script == pending_store._TAKE_SCRIPT else 0
        note = json.loads(encoded)
        if script == pending_store._TAKE_SCRIPT:
            await self.delete(key)
            return encoded
        if script == pending_store._TAKE_IF_MATCHES_SCRIPT:
            interaction_id, version = args
            if (
                note["interaction_id"] != interaction_id
                or note["version"] != int(version)
            ):
                return 0
            await self.delete(key)
            return 1
        if script == pending_store._UPDATE_SCRIPT:
            interaction_id, version, next_encoded, ttl = args
            if (
                note["interaction_id"] != interaction_id
                or note["version"] != int(version)
            ):
                return 0
            await self.set(key, next_encoded, int(ttl))
            return 1
        raise AssertionError("Unexpected Redis script")


@pytest.fixture
def select_redis_backend(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: redis,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())
    yield redis
    asyncio.run(pending_store.close_pending_interaction_store())


def test_redis_save_take_is_once_and_conversation_scoped(select_redis_backend):
    async def run():
        await pending_store.store_pending_interaction(
            "conversation-a",
            "clarification",
            {"original_question": "private question"},
        )
        note = await pending_store.get_pending_interaction("conversation-a")
        assert note["payload"]["original_question"] == "private question"
        assert await pending_store.get_pending_interaction("conversation-b") is None
        assert (
            await pending_store.consume_pending_interaction("conversation-a")
        )["type"] == "clarification"
        assert await pending_store.consume_pending_interaction("conversation-a") is None

    asyncio.run(run())


def test_concurrent_redis_updates_with_same_version_only_succeed_once(
    select_redis_backend,
):
    async def run():
        await pending_store.store_pending_interaction(
            "conversation-a",
            "clarification",
            {"invalid_replies": 0},
        )
        note = await pending_store.get_pending_interaction("conversation-a")
        results = await asyncio.gather(
            pending_store.update_pending_interaction(
                "conversation-a",
                note["interaction_id"],
                note["version"],
                {"invalid_replies": 1},
            ),
            pending_store.update_pending_interaction(
                "conversation-a",
                note["interaction_id"],
                note["version"],
                {"invalid_replies": 1},
            ),
        )
        assert sorted(results) == [False, True]
        updated = await pending_store.get_pending_interaction("conversation-a")
        assert updated["version"] == 2

    asyncio.run(run())


def test_redis_reads_do_not_extend_ttl_but_valid_update_does(
    select_redis_backend,
    monkeypatch,
):
    monkeypatch.setattr(pending_store, "_ttl", lambda _interaction_type: 10)

    async def run():
        await pending_store.store_pending_interaction(
            "conversation-a",
            "clarification",
            {"invalid_replies": 0},
        )
        key = pending_store._redis_key("conversation-a")
        first_expiry = select_redis_backend.expiry[key]
        select_redis_backend.clock += 2
        await pending_store.get_pending_interaction("conversation-a")
        assert select_redis_backend.expiry[key] == first_expiry

        note = await pending_store.get_pending_interaction("conversation-a")
        assert await pending_store.update_pending_interaction(
            "conversation-a",
            note["interaction_id"],
            note["version"],
            {"invalid_replies": 1},
        )
        assert select_redis_backend.expiry[key] == select_redis_backend.clock + 10
        select_redis_backend.clock += 10
        assert await pending_store.get_pending_interaction("conversation-a") is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "operation",
    ["save", "read", "update", "take", "consume_if_matches"],
)
def test_production_redis_down_raises_without_memory_fallback(
    operation,
    monkeypatch,
):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: None,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())

    async def run():
        with pytest.raises(pending_store.StorageUnavailable):
            if operation == "save":
                await pending_store.store_pending_interaction(
                    "conversation-a", "clarification", {}
                )
            elif operation == "read":
                await pending_store.get_pending_interaction("conversation-a")
            elif operation == "update":
                await pending_store.update_pending_interaction(
                    "conversation-a", "note-id", 1, {}
                )
            elif operation == "consume_if_matches":
                await pending_store.consume_pending_interaction_if_matches(
                    "conversation-a", "note-id", 1
                )
            else:
                await pending_store.consume_pending_interaction(
                    "conversation-a"
                )

    try:
        asyncio.run(run())
        assert pending_store._backend == "redis"
        assert not pending_store._pending
    finally:
        asyncio.run(pending_store.close_pending_interaction_store())


def test_unset_app_env_uses_production_backend(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: None,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())

    try:
        assert pending_store._backend == "redis"
        with pytest.raises(pending_store.StorageUnavailable):
            asyncio.run(
                pending_store.get_pending_interaction("conversation-a")
            )
        assert not pending_store._pending
    finally:
        asyncio.run(pending_store.close_pending_interaction_store())


def test_development_redis_down_uses_memory_and_warns_once(
    monkeypatch,
    caplog,
):
    redis = FakeRedis(unavailable=True)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: redis,
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(pending_store.initialize_pending_interaction_store())
        asyncio.run(pending_store.store_pending_interaction(
            "conversation-a", "clarification", {"original_question": "q"}
        ))
        assert asyncio.run(
            pending_store.get_pending_interaction("conversation-a")
        )

    warnings = [
        record for record in caplog.records
        if "pending_interaction_memory_fallback" in record.message
    ]
    assert len(warnings) == 1
    assert "backend=memory" in warnings[0].message
    asyncio.run(pending_store.close_pending_interaction_store())


def test_development_backend_does_not_switch_after_startup(monkeypatch):
    redis = FakeRedis(unavailable=True)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: redis,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())
    redis.unavailable = False

    async def run():
        await pending_store.store_pending_interaction(
            "conversation-a",
            "clarification",
            {"original_question": "q"},
        )
        assert pending_store._redis_key("conversation-a") not in redis.values
        assert await pending_store.get_pending_interaction("conversation-a")

    try:
        assert pending_store._backend == "memory"
        asyncio.run(run())
    finally:
        asyncio.run(pending_store.close_pending_interaction_store())


def test_production_read_failure_returns_try_again_message(monkeypatch):
    redis = FakeRedis(unavailable=True)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: redis,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())

    async def no_suggestion_cleanup(_conversation_id):
        return None

    monkeypatch.setattr(chat, "clear_suggestion_plans", no_suggestion_cleanup)
    try:
        response = asyncio.run(chat.chat(chat.ChatRequest(
            question="yes",
            conversation_id="e6a3bb54-c115-4f9d-8a09-ef227a265581",
        )))
        assert response.status_code == 503
        body = json.loads(response.body)
        assert body["status"] == "storage_unavailable"
        assert body["message"] == (
            "We couldn't process your reply right now. Please try again."
        )
    finally:
        asyncio.run(pending_store.close_pending_interaction_store())


def test_production_save_failure_does_not_return_clarification(monkeypatch):
    class SaveFailsRedis(FakeRedis):
        async def get(self, key):
            return None

        async def set(self, key, value, ex):
            raise RedisError("simulated redis outage")

    redis = SaveFailsRedis()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: redis,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())

    async def no_suggestion_cleanup(_conversation_id):
        return None

    async def clarification_pipeline(question, **_kwargs):
        return {
            "user_question": question,
            "final_answer": {
                "clarification_question": "Which shop type?",
                "clarification_options": [],
            },
        }

    monkeypatch.setattr(chat, "clear_suggestion_plans", no_suggestion_cleanup)
    monkeypatch.setattr(chat, "run_pipeline", clarification_pipeline)
    try:
        response = asyncio.run(chat.chat(chat.ChatRequest(
            question="Show an unknown shop type",
            conversation_id="e6a3bb54-c115-4f9d-8a09-ef227a265581",
        )))
        assert response.status_code == 503
        body = json.loads(response.body)
        assert body["status"] == "storage_unavailable"
        assert body["message"] == (
            "We couldn't process your reply right now. Please try again."
        )
    finally:
        asyncio.run(pending_store.close_pending_interaction_store())


def test_storage_failure_logs_do_not_include_question_or_unclear_phrase(
    monkeypatch,
    caplog,
):
    redis = FakeRedis(unavailable=True)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: redis,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())
    question = "SECRET question text"
    phrase = "SECRET unclear phrase"
    with caplog.at_level(logging.WARNING):
        with pytest.raises(pending_store.StorageUnavailable):
            asyncio.run(pending_store.store_pending_interaction(
                "conversation-a",
                "clarification",
                {
                    "original_question": question,
                    "unclear_phrase": phrase,
                },
            ))

    assert question not in caplog.text
    assert phrase not in caplog.text
    assert "operation=save backend=redis" in caplog.text
    asyncio.run(pending_store.close_pending_interaction_store())
