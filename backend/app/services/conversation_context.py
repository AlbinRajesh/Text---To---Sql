"""Bounded, process-local context for single-user development conversations.

This store is not suitable for multi-user or multi-worker production use.
Add authenticated user/tenant scoping and a shared store before deploying it
where conversation IDs can cross user boundaries.
"""

from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import logging
import threading
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

CONVERSATION_CONTEXT_TTL_SECONDS = 30 * 60
MAX_CONVERSATIONS = 256
MAX_CONTEXT_TURNS = 4
MAX_CONTEXT_ROWS = 4
MAX_CONTEXT_COLUMNS = 8
MAX_CONTEXT_VALUE_LENGTH = 256
MAX_CONTEXT_STRUCTURE_ITEMS = 24
_contexts: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_lock = threading.RLock()
_UNSUPPORTED = object()


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_CONTEXT_VALUE_LENGTH else _UNSUPPORTED
    if value is None or type(value) in {int, float, bool}:
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return _UNSUPPORTED


def _safe_structure(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    safe_value = _safe_scalar(value)
    if safe_value is not _UNSUPPORTED:
        return safe_value
    if isinstance(value, dict):
        return {
            key: child
            for key, item in list(value.items())[:MAX_CONTEXT_STRUCTURE_ITEMS]
            if isinstance(key, str)
            and (child := _safe_structure(item, depth + 1)) is not _UNSUPPORTED
        }
    if isinstance(value, (list, tuple)):
        return [
            child
            for item in value[:MAX_CONTEXT_STRUCTURE_ITEMS]
            if (child := _safe_structure(item, depth + 1)) is not _UNSUPPORTED
        ]
    return _UNSUPPORTED


def _prune_expired(now: float) -> None:
    expired = [
        key for key, value in _contexts.items()
        if now - value["updated_at"] >= CONVERSATION_CONTEXT_TTL_SECONDS
    ]
    for key in expired:
        del _contexts[key]


def get_conversation_context(conversation_id: str) -> List[Dict[str, Any]]:
    """Return a copy of the latest verified result context for this conversation."""
    now = time.monotonic()
    with _lock:
        _prune_expired(now)
        entry = _contexts.get(conversation_id)
        if entry is None:
            return []
        entry["updated_at"] = now
        _contexts.move_to_end(conversation_id)
        return deepcopy(entry["turns"])


def store_verified_turn(
    conversation_id: str,
    *,
    columns: List[str],
    rows: List[Dict[str, Any]],
    metrics: List[Dict[str, Any]],
    filters: Any,
    query: Any = None,
) -> None:
    """Store only bounded, structured data from a result that passed validation."""
    safe_columns = [
        column for column in columns[:MAX_CONTEXT_COLUMNS]
        if isinstance(column, str)
    ]
    if not safe_columns or not rows:
        return
    entry = {
        "columns": safe_columns,
        "rows": [
            {
                key: safe_value
                for key, value in row.items()
                if key in safe_columns
                and (safe_value := _safe_scalar(value)) is not _UNSUPPORTED
            }
            for row in rows[:MAX_CONTEXT_ROWS]
            if isinstance(row, dict)
        ],
        "metrics": [
            {
                key: item[key]
                for key in (
                    "table", "column", "aggregate", "alias", "condition",
                    "period", "period_comparison", "date_bucket",
                )
                if key in item
            }
            for item in metrics
            if isinstance(item, dict)
        ][:MAX_CONTEXT_COLUMNS],
        "filters": _safe_structure(filters),
        "query": _safe_structure(query),
    }
    now = time.monotonic()
    with _lock:
        _prune_expired(now)
        existing = _contexts.get(conversation_id, {"turns": []})
        turns = existing["turns"]
        turns.append(entry)
        existing["turns"] = turns[-MAX_CONTEXT_TURNS:]
        existing["updated_at"] = now
        _contexts[conversation_id] = existing
        _contexts.move_to_end(conversation_id)
        while len(_contexts) > MAX_CONVERSATIONS:
            _contexts.popitem(last=False)
    logger.debug(
        "Stored verified context for conversation (%d columns, %d rows).",
        len(entry["columns"]),
        len(entry["rows"]),
    )


def clear_conversation_context(conversation_id: str) -> None:
    """Remove context for a conversation, for lifecycle cleanup and tests."""
    with _lock:
        _contexts.pop(conversation_id, None)


def clear_all_conversation_contexts() -> None:
    """Clear the process-local store; intended for tests and application shutdown."""
    with _lock:
        _contexts.clear()
