from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

from app.services.errors import InfrastructureError
from app.tests.e2e_frontend.utils.sse_capture import parse_stream_events, stream_result


class ChatStreamError(RuntimeError):
    """Raised when the chat stream endpoint cannot return a valid result."""


CHAT_STREAM_MAX_RETRIES = 3
CHAT_STREAM_BASE_BACKOFF_SECONDS = 1.0
CHAT_STREAM_MAX_RETRY_AFTER_SECONDS = 30.0
logger = logging.getLogger(__name__)


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def post_chat_stream(
    backend_url: str,
    question: str,
    *,
    conversation_id: str | None = None,
    timeout: float = 100.0,
) -> dict[str, Any]:
    """Call the API directly and return parsed stream events and its result."""
    request_id = str(uuid4())
    payload: dict[str, str] = {"question": question}
    if conversation_id:
        payload["conversation_id"] = conversation_id

    started = time.perf_counter()
    raw: bytes | None = None
    status = 0
    for attempt in range(1, CHAT_STREAM_MAX_RETRIES + 1):
        request = urllib.request.Request(
            f"{backend_url.rstrip('/')}/api/chat/stream",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                if attempt == CHAT_STREAM_MAX_RETRIES:
                    raise InfrastructureError(
                        "CHAT_HTTP_429",
                        "HTTP 429 persisted after bounded retries.",
                        stage="chat_stream",
                    ) from exc
                retry_after = _retry_after_seconds(exc)
                if (
                    retry_after is not None
                    and retry_after > CHAT_STREAM_MAX_RETRY_AFTER_SECONDS
                ):
                    raise InfrastructureError(
                        "CHAT_HTTP_429",
                        "HTTP 429 Retry-After exceeds the test runner's retry budget.",
                        stage="chat_stream",
                    ) from exc
                delay = (
                    retry_after
                    if retry_after is not None
                    else CHAT_STREAM_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                )
                logger.warning(
                    "Chat stream rate limited; retrying attempt %d/%d in %.2fs",
                    attempt + 1,
                    CHAT_STREAM_MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)
                continue
            body = exc.read().decode("utf-8", "replace")
            raise ChatStreamError(
                f"/api/chat/stream returned HTTP {exc.code}: {body[:1000]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, OSError)):
                code = (
                    "CHAT_TIMEOUT"
                    if isinstance(reason, TimeoutError)
                    else "CHAT_CONNECTION_ERROR"
                )
                raise InfrastructureError(
                    code,
                    f"Could not complete /api/chat/stream request: {reason}",
                    stage="chat_stream",
                ) from exc
            raise ChatStreamError(f"Could not reach /api/chat/stream: {exc}") from exc

    if raw is None:
        raise InfrastructureError(
            "CHAT_HTTP_429",
            "No response received after rate-limit retries.",
            stage="chat_stream",
        )

    if status >= 400:
        raise ChatStreamError(f"/api/chat/stream returned HTTP {status}.")

    events = parse_stream_events(raw, status)
    result = stream_result(events)
    if result is None:
        raise ChatStreamError("The stream ended without a result event.")
    return {
        "request_id": result.get("request_id", request_id),
        "events": events,
        "result": result,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }
