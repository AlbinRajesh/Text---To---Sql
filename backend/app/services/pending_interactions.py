"""Short-lived, conversation-scoped confirmation and clarification state."""

from collections import OrderedDict
from copy import deepcopy
import threading
import time
from typing import Any, Dict, Optional

PENDING_INTERACTION_TTL_SECONDS = 5 * 60
MAX_PENDING_INTERACTIONS = 512
_pending: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_lock = threading.RLock()


def _prune_expired(now: float) -> None:
    expired = [
        conversation_id
        for conversation_id, entry in _pending.items()
        if now - entry["updated_at"] >= PENDING_INTERACTION_TTL_SECONDS
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
    with _lock:
        _prune_expired(now)
        _pending[conversation_id] = {
            "type": interaction_type,
            "payload": deepcopy(payload),
            "updated_at": now,
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
        _pending.move_to_end(conversation_id)
        return {
            "type": entry["type"],
            "payload": deepcopy(entry["payload"]),
        }


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
