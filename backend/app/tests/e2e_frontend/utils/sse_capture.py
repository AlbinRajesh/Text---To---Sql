from __future__ import annotations

import json
from typing import Any


class RateLimitError(RuntimeError):
    pass


def parse_stream_events(raw: bytes | str, http_status: int) -> list[dict[str, Any]]:
    if http_status == 429:
        raise RateLimitError("HTTP 429 received from /api/chat/stream; stop without retrying.")

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        payload = line.strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if event.get("status") == 429 or event.get("error") == "Too Many Requests":
            raise RateLimitError(
                "429 Too Many Requests received in the chat stream; stop without retrying."
            )
        events.append(event)
    return events


def stream_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return None


def stream_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = stream_result(events)
    if not result:
        return []
    return list(result.get("results") or [])
