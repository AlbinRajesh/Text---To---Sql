"""Short-lived, one-time suggestion plans shared by API workers through Redis."""

import hashlib
import json
import logging
import os
import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

SUGGESTION_TTL_SECONDS = 180
MAX_SUGGESTION_PLANS = 3
DEFAULT_SUGGESTION_REDIS_URL = "redis://localhost:6379/0"
_redis: Optional[Redis] = None


def get_suggestion_redis_client() -> Optional[Redis]:
    """Return the shared Redis client for other short-lived stores."""
    return _redis


_CONSUME_SCRIPT = """
local encoded = redis.call("GET", KEYS[1])
if not encoded then
    return {0}
end
local bundle = cjson.decode(encoded)
if bundle.conversation_id ~= ARGV[1] then
    return {-1}
end
local plan = bundle.plans[ARGV[2]]
if not plan then
    return {-1}
end
redis.call("DEL", KEYS[1])
return {1, cjson.encode(plan)}
"""


class SuggestionStoreUnavailableError(RuntimeError):
    """Raised when the configured shared suggestion store cannot be used."""


class SuggestionNotFoundError(LookupError):
    """Raised when a suggestion token is expired, invalid, or already consumed."""


def _redis_key(conversation_id: str) -> str:
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
    return f"nosql-agent:suggestion-plans:{digest}"


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8")
    raise TypeError(f"Unsupported suggestion-plan value: {type(value).__name__}")


async def initialize_suggestion_store() -> None:
    """Connect to Redis; use the local development address when none is configured."""
    global _redis
    url = os.getenv("SUGGESTION_REDIS_URL", DEFAULT_SUGGESTION_REDIS_URL)
    if not os.getenv("SUGGESTION_REDIS_URL"):
        logger.info(
            "SUGGESTION_REDIS_URL is unset; trying the local Redis default."
        )

    client: Optional[Redis] = None
    try:
        client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
        await client.ping()
    except (RedisError, ValueError):
        if client is not None:
            await client.aclose()
        logger.exception(
            "Could not connect to Redis; fast suggestion redemption is disabled."
        )
        _redis = None
        return
    _redis = client
    logger.info("Shared suggestion-plan Redis store is ready.")


async def suggestion_fast_path_available() -> bool:
    """Check Redis reachability for the API health response."""
    if _redis is None:
        return False
    try:
        return bool(await _redis.ping())
    except RedisError:
        logger.warning("Redis is unreachable; fast suggestion execution is unavailable.")
        return False


async def close_suggestion_store() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def _require_redis() -> Redis:
    if _redis is None:
        raise SuggestionStoreUnavailableError(
            "Fast suggestion redemption is unavailable because Redis is not configured."
        )
    return _redis


async def store_suggestion_plans(
    conversation_id: str,
    plans: List[Dict[str, Any]],
) -> List[str]:
    """Replace this conversation's pending choices and return their opaque tokens."""
    if not plans:
        return []
    if len(plans) > MAX_SUGGESTION_PLANS:
        raise ValueError(f"At most {MAX_SUGGESTION_PLANS} suggestion plans may be stored.")

    client = _require_redis()
    tokens = [secrets.token_urlsafe(32) for _ in plans]
    bundle = {
        "conversation_id": conversation_id,
        "plans": dict(zip(tokens, plans)),
    }
    try:
        await client.set(
            _redis_key(conversation_id),
            json.dumps(bundle, default=_json_default, separators=(",", ":")),
            ex=SUGGESTION_TTL_SECONDS,
        )
    except (RedisError, TypeError, ValueError) as exc:
        raise SuggestionStoreUnavailableError(
            "Could not store suggestion plans."
        ) from exc
    return tokens


async def consume_suggestion_plan(
    conversation_id: str,
    suggestion_id: str,
) -> Dict[str, Any]:
    """Atomically validate and consume one choice; consuming invalidates its siblings."""
    client = _require_redis()
    try:
        response = await client.eval(
            _CONSUME_SCRIPT,
            1,
            _redis_key(conversation_id),
            conversation_id,
            suggestion_id,
        )
    except (RedisError, TypeError, ValueError) as exc:
        raise SuggestionStoreUnavailableError(
            "Could not redeem the suggestion plan."
        ) from exc

    if not response or int(response[0]) != 1:
        raise SuggestionNotFoundError(
            "The suggestion has expired, is invalid, or has already been used."
        )
    return json.loads(response[1])


async def clear_suggestion_plans(conversation_id: str) -> None:
    """Invalidate all pending choices for a conversation."""
    if _redis is None:
        return
    try:
        await _redis.delete(_redis_key(conversation_id))
    except RedisError:
        logger.exception(
            "Could not clear pending suggestion plans for a conversation."
        )
