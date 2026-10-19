import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.services import pending_interactions as pending_store
from app.services.suggestion_store import DEFAULT_SUGGESTION_REDIS_URL


def test_pending_interaction_storage_against_real_redis(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        pending_store,
        "PENDING_CLARIFICATION_TTL_SECONDS",
        20,
    )

    async def run():
        client = Redis.from_url(
            os.getenv(
                "SUGGESTION_REDIS_URL",
                DEFAULT_SUGGESTION_REDIS_URL.replace(
                    "localhost", "127.0.0.1"
                ),
            ),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        try:
            try:
                await client.ping()
            except (RedisError, ValueError) as exc:
                pytest.skip(f"Real Redis is unreachable: {exc}")

            monkeypatch.setattr(
                pending_store,
                "get_suggestion_redis_client",
                lambda: client,
            )
            await pending_store.initialize_pending_interaction_store()
            take_conversation = f"integration-take-{uuid4().hex}"
            update_conversation = f"integration-update-{uuid4().hex}"
            ttl_conversation = f"integration-ttl-{uuid4().hex}"
            conversations = (
                take_conversation,
                update_conversation,
                ttl_conversation,
            )

            try:
                await pending_store.store_pending_interaction(
                    take_conversation,
                    "clarification",
                    {"question": "take once"},
                )
                taken = await pending_store.consume_pending_interaction(
                    take_conversation
                )
                assert taken["payload"]["question"] == "take once"
                assert await pending_store.consume_pending_interaction(
                    take_conversation
                ) is None

                await pending_store.store_pending_interaction(
                    update_conversation,
                    "clarification",
                    {"invalid_replies": 0},
                )
                note = await pending_store.get_pending_interaction(
                    update_conversation
                )
                update_key = pending_store._redis_key(update_conversation)
                await asyncio.sleep(1)
                ttl_before = await client.pttl(update_key)
                results = await asyncio.gather(
                    pending_store.update_pending_interaction(
                        update_conversation,
                        note["interaction_id"],
                        note["version"],
                        {"invalid_replies": 1},
                    ),
                    pending_store.update_pending_interaction(
                        update_conversation,
                        note["interaction_id"],
                        note["version"],
                        {"invalid_replies": 2},
                    ),
                )
                assert sorted(results) == [False, True]
                ttl_after = await client.pttl(update_key)
                assert ttl_after > ttl_before
                assert ttl_after <= 20_000

                await pending_store.store_pending_interaction(
                    ttl_conversation,
                    "clarification",
                    {"question": "check TTL"},
                )
                ttl_key = pending_store._redis_key(ttl_conversation)
                ttl_on_save_ms = await client.pttl(ttl_key)
                assert 0 < ttl_on_save_ms <= 20_000
                await asyncio.sleep(0.15)
                assert await pending_store.get_pending_interaction(
                    ttl_conversation
                ) is not None
                ttl_after_read_ms = await client.pttl(ttl_key)
                assert 0 < ttl_after_read_ms < ttl_on_save_ms
            finally:
                for conversation_id in conversations:
                    await client.delete(
                        pending_store._redis_key(conversation_id)
                    )
                await pending_store.close_pending_interaction_store()
        finally:
            await client.aclose()

    asyncio.run(run())
