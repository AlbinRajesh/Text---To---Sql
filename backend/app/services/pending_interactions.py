"""Short-lived, conversation-scoped confirmation and clarification state."""

from collections import OrderedDict
from copy import deepcopy
import threading
import time
from typing import Any, Dict, Optional
from uuid import uuid4

PENDING_CONFIRMATION_TTL_SECONDS = 5 * 60
PENDING_CLARIFICATION_TTL_SECONDS = 10 * 60
MAX_PENDING_INTERACTIONS = 512
_pending: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_lock = threading.RLock()


def _prune_expired(now: float) -> None:
    expired = [
        conversation_id
        for conversation_id, entry in _pending.items()
        if now - entry["updated_at"] >= entry["ttl_seconds"]
    ]
    for conversation_id in expired:
        del _pending[conversation_id]


def store_pending_interaction(
    conversation_id: str,
    interaction_type: str,
    payload: Dict[str, Any],
) -> None:
    if interaction_type not in {"sql_confirmation", "clarification"}:
        raise ValueError(f"Unsupported pending interaction: {interaction_type!r}")
    now = time.monotonic()
    ttl_seconds = (
        PENDING_CLARIFICATION_TTL_SECONDS
        if interaction_type == "clarification"
        else PENDING_CONFIRMATION_TTL_SECONDS
    )
    copied_payload = deepcopy(payload)
    interaction_id = uuid4().hex
    version = 1
    copied_payload.update({
        "conversation_id": conversation_id,
        "note_id": interaction_id,
        "version": version,
        "expires_at": time.time() + ttl_seconds,
    })
    with _lock:
        _prune_expired(now)
        _pending[conversation_id] = {
            "type": interaction_type,
            "payload": copied_payload,
            "updated_at": now,
            "ttl_seconds": ttl_seconds,
            "interaction_id": interaction_id,
            "version": version,
        }
        _pending.move_to_end(conversation_id)
        while len(_pending) > MAX_PENDING_INTERACTIONS:
            _pending.popitem(last=False)


def get_pending_interaction(conversation_id: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _lock:
        _prune_expired(now)
        entry = _pending.get(conversation_id)
        if entry is None:
            return None
        entry["updated_at"] = now
        entry["payload"]["expires_at"] = time.time() + entry["ttl_seconds"]
        _pending.move_to_end(conversation_id)
        return {
            "type": entry["type"],
            "payload": deepcopy(entry["payload"]),
            "interaction_id": entry["interaction_id"],
            "version": entry["version"],
        }


def update_pending_interaction(
    conversation_id: str,
    interaction_id: str,
    version: int,
    payload: Dict[str, Any],
) -> bool:
    """Atomically update one claimed interaction version."""
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
        next_version = version + 1
        ttl_seconds = entry["ttl_seconds"]
        copied_payload = deepcopy(payload)
        copied_payload.update({
            "conversation_id": conversation_id,
            "note_id": interaction_id,
            "version": next_version,
            "expires_at": time.time() + ttl_seconds,
        })
        entry.update({
            "payload": copied_payload,
            "updated_at": now,
            "version": next_version,
        })
        _pending.move_to_end(conversation_id)
        return True


def consume_pending_interaction_if_matches(
    conversation_id: str,
    interaction_id: str,
    version: int,
) -> bool:
    """Atomically consume a pending interaction only if its version is current."""
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


def consume_pending_interaction(
    conversation_id: str,
) -> Optional[Dict[str, Any]]:
    """Atomically return and remove an interaction before its response is handled."""
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


def clear_pending_interaction(conversation_id: str) -> None:
    with _lock:
        _pending.pop(conversation_id, None)


def clear_all_pending_interactions() -> None:
    with _lock:
        _pending.clear()
