"""Short-lived, conversation-scoped confirmation and clarification state."""

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from typing import Any, Dict, Optional
from uuid import uuid4

from redis.exceptions import RedisError

from app.services.suggestion_store import get_suggestion_redis_client

logger = logging.getLogger(__name__)

PENDING_CONFIRMATION_TTL_SECONDS = 5 * 60
PENDING_CLARIFICATION_TTL_SECONDS = int(
    os.getenv("PENDING_CLARIFICATION_TTL_SECONDS", "600")
)
PENDING_INTERACTION_REDIS_PREFIX = "nosql-agent:pending-interactions:"
MAX_PENDING_INTERACTIONS = 512

_pending: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_lock = threading.RLock()
_backend: str | None = None

_TAKE_SCRIPT = """
local encoded = redis.call("GET", KEYS[1])
if not encoded then
    return false
end
redis.call("DEL", KEYS[1])
return encoded
"""

_TAKE_IF_MATCHES_SCRIPT = """
local encoded = redis.call("GET", KEYS[1])
if not encoded then
    return 0
end
local note = cjson.decode(encoded)
if note.interaction_id ~= ARGV[1] or tonumber(note.version) ~= tonumber(ARGV[2]) then
    return 0
end
redis.call("DEL", KEYS[1])
return 1
"""

_UPDATE_SCRIPT = """
local encoded = redis.call("GET", KEYS[1])
if not encoded then
    return 0
end
local note = cjson.decode(encoded)
if note.interaction_id ~= ARGV[1] or tonumber(note.version) ~= tonumber(ARGV[2]) then
    return 0
end
redis.call("SET", KEYS[1], ARGV[3], "EX", tonumber(ARGV[4]))
return 1
"""


class StorageUnavailable(RuntimeError):
    """The configured pending-interaction backend cannot be used."""


def _redis_key(conversation_id: str) -> str:
    return f"{PENDING_INTERACTION_REDIS_PREFIX}{conversation_id}"


def _require_backend() -> str:
    if _backend is None:
        raise StorageUnavailable("Pending interaction storage is not initialized.")
    return _backend


def _encode(note: Dict[str, Any]) -> str:
    try:
        return json.dumps(note, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StorageUnavailable("Pending interaction could not be serialized.") from exc


def _decode(encoded: Any) -> Optional[Dict[str, Any]]:
    if encoded is None or encoded is False:
        return None
    try:
        note = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise StorageUnavailable("Pending interaction data is invalid.") from exc
    if not isinstance(note, dict):
        raise StorageUnavailable("Pending interaction data is invalid.")
    return note


def _raise_unavailable(operation: str) -> None:
    logger.warning(
        "pending_interaction_storage_unavailable operation=%s backend=redis",
        operation,
    )
    raise StorageUnavailable("Pending interaction storage is unavailable.")


async def initialize_pending_interaction_store() -> None:
    """Select one backend at startup; never switch backends during requests."""
    global _backend
    environment = os.getenv("APP_ENV", "production").strip().casefold()
    if environment not in {"development", "production"}:
        raise ValueError("APP_ENV must be 'development' or 'production'.")
    if PENDING_CLARIFICATION_TTL_SECONDS < 1:
        raise ValueError("PENDING_CLARIFICATION_TTL_SECONDS must be positive.")

    client = get_suggestion_redis_client()
    if environment == "development":
        if client is not None:
            try:
                await client.ping()
            except RedisError:
                client = None
        if client is None:
            _backend = "memory"
            logger.warning(
                "pending_interaction_memory_fallback reason=redis_unavailable "
                "backend=memory"
            )
            return
    _backend = "redis"


async def close_pending_interaction_store() -> None:
    """Stop serving operations after application shutdown."""
    global _backend
    _backend = None
    with _lock:
        _pending.clear()


def _ttl(interaction_type: str) -> int:
    return (
        PENDING_CLARIFICATION_TTL_SECONDS
        if interaction_type == "clarification"
        else PENDING_CONFIRMATION_TTL_SECONDS
    )


def _new_note(
    conversation_id: str,
    interaction_type: str,
    payload: Dict[str, Any],
    version: int = 1,
    interaction_id: str | None = None,
) -> Dict[str, Any]:
    ttl_seconds = _ttl(interaction_type)
    copied_payload = deepcopy(payload)
    note_id = interaction_id or uuid4().hex
    copied_payload.update({
        "conversation_id": conversation_id,
        "note_id": note_id,
        "version": version,
        "expires_at": time.time() + ttl_seconds,
    })
    return {
        "type": interaction_type,
        "payload": copied_payload,
        "updated_at": time.monotonic(),
        "ttl_seconds": ttl_seconds,
        "interaction_id": note_id,
        "version": version,
    }


def _prune_expired(now: float) -> None:
    expired = [
        conversation_id
        for conversation_id, entry in _pending.items()
        if now - entry["updated_at"] >= entry["ttl_seconds"]
    ]
    for conversation_id in expired:
        del _pending[conversation_id]


async def store_pending_interaction(
    conversation_id: str,
    interaction_type: str,
    payload: Dict[str, Any],
) -> None:
    if interaction_type not in {"sql_confirmation", "clarification"}:
        raise ValueError(f"Unsupported pending interaction: {interaction_type!r}")
    backend = _require_backend()
    note = _new_note(conversation_id, interaction_type, payload)
    if backend == "redis":
        client = get_suggestion_redis_client()
        if client is None:
            _raise_unavailable("save")
        try:
            await client.set(
                _redis_key(conversation_id),
                _encode(note),
                ex=note["ttl_seconds"],
            )
        except (RedisError, TypeError, ValueError):
            _raise_unavailable("save")
        return

    with _lock:
        _prune_expired(note["updated_at"])
        _pending[conversation_id] = note
        _pending.move_to_end(conversation_id)
        while len(_pending) > MAX_PENDING_INTERACTIONS:
            _pending.popitem(last=False)


async def get_pending_interaction(
    conversation_id: str,
) -> Optional[Dict[str, Any]]:
    backend = _require_backend()
    if backend == "redis":
        client = get_suggestion_redis_client()
        if client is None:
            _raise_unavailable("read")
        try:
            encoded = await client.get(_redis_key(conversation_id))
        except RedisError:
            _raise_unavailable("read")
        note = _decode(encoded)
        if note is None:
            return None
        return {
            "type": note["type"],
            "payload": deepcopy(note["payload"]),
            "interaction_id": note["interaction_id"],
            "version": note["version"],
        }

    now = time.monotonic()
    with _lock:
        _prune_expired(now)
        entry = _pending.get(conversation_id)
        if entry is None:
            return None
        _pending.move_to_end(conversation_id)
        return {
            "type": entry["type"],
            "payload": deepcopy(entry["payload"]),
            "interaction_id": entry["interaction_id"],
            "version": entry["version"],
        }


async def update_pending_interaction(
    conversation_id: str,
    interaction_id: str,
    version: int,
    payload: Dict[str, Any],
) -> bool:
    """Atomically update one claimed interaction version and refresh its TTL."""
    backend = _require_backend()
    if backend == "redis":
        client = get_suggestion_redis_client()
        if client is None:
            _raise_unavailable("update")
        try:
            current = _decode(await client.get(_redis_key(conversation_id)))
        except RedisError:
            _raise_unavailable("update")
        if current is None:
            return False
        next_note = _new_note(
            conversation_id,
            current["type"],
            payload,
            version=version + 1,
            interaction_id=interaction_id,
        )
        try:
            result = await client.eval(
                _UPDATE_SCRIPT,
                1,
                _redis_key(conversation_id),
                interaction_id,
                version,
                _encode(next_note),
                next_note["ttl_seconds"],
            )
        except (RedisError, TypeError, ValueError):
            _raise_unavailable("update")
        return result == 1

    now = time.monotonic()
    with _lock:
        _prune_expired(now)
        entry = _pending.get(conversation_id)
        if (
            entry is None
            or entry["interaction_id"] != interaction_id
            or entry["version"] != version
        ):
            return False
        updated = _new_note(
            conversation_id,
            entry["type"],
            payload,
            version=version + 1,
            interaction_id=interaction_id,
        )
        _pending[conversation_id] = updated
        _pending.move_to_end(conversation_id)
        return True


async def consume_pending_interaction_if_matches(
    conversation_id: str,
    interaction_id: str,
    version: int,
) -> bool:
    """Atomically consume a pending interaction only if its version is current."""
    backend = _require_backend()
    if backend == "redis":
        client = get_suggestion_redis_client()
        if client is None:
            _raise_unavailable("consume")
        try:
            result = await client.eval(
                _TAKE_IF_MATCHES_SCRIPT,
                1,
                _redis_key(conversation_id),
                interaction_id,
                version,
            )
        except (RedisError, TypeError, ValueError):
            _raise_unavailable("consume")
        return result == 1

    now = time.monotonic()
    with _lock:
        _prune_expired(now)
        entry = _pending.get(conversation_id)
        if (
            entry is None
            or entry["interaction_id"] != interaction_id
            or entry["version"] != version
        ):
            return False
        del _pending[conversation_id]
        return True


async def consume_pending_interaction(
    conversation_id: str,
) -> Optional[Dict[str, Any]]:
    """Atomically return and remove an interaction before its response is handled."""
    backend = _require_backend()
    if backend == "redis":
        client = get_suggestion_redis_client()
        if client is None:
            _raise_unavailable("take")
        try:
            encoded = await client.eval(
                _TAKE_SCRIPT,
                1,
                _redis_key(conversation_id),
            )
        except RedisError:
            _raise_unavailable("take")
        note = _decode(encoded)
        if note is None:
            return None
        return {"type": note["type"], "payload": deepcopy(note["payload"])}

    now = time.monotonic()
    with _lock:
        _prune_expired(now)
        entry = _pending.pop(conversation_id, None)
        if entry is None:
            return None
        return {
            "type": entry["type"],
            "payload": deepcopy(entry["payload"]),
        }


async def clear_pending_interaction(conversation_id: str) -> None:
    backend = _require_backend()
    if backend == "redis":
        client = get_suggestion_redis_client()
        if client is None:
            _raise_unavailable("clear")
        try:
            await client.delete(_redis_key(conversation_id))
        except RedisError:
            _raise_unavailable("clear")
        return
    with _lock:
        _pending.pop(conversation_id, None)


def clear_all_pending_interactions() -> None:
    """Clear process-local entries; intended for in-memory tests."""
    with _lock:
        _pending.clear()
