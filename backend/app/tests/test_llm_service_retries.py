"""Retry-owner tests for llm_service. No real API calls, no real sleeping."""

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from groq import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from app.services import llm_service
from app.services.llm_service import (
    AgentConfig,
    LLMService,
    LLMServiceError,
    _retry_after_seconds,
    _safe_headers,
)

_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
_MESSAGES = [{"role": "user", "content": "hi"}]


def _response(status, headers=None):
    return httpx.Response(
        status,
        headers=headers or {},
        request=_REQUEST,
        json={"error": {"message": "rate limited"}},
    )


def _rate_limit(headers=None):
    return RateLimitError("rate limited", response=_response(429, headers), body=None)


def _status_error(status):
    return APIStatusError("boom", response=_response(status), body=None)


@pytest.fixture
def env(monkeypatch):
    """Fixed settings, zero jitter, no real sleeping, captured records."""
    sleeps, records = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(llm_service.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(llm_service, "record_llm_call", lambda **kw: records.append(kw))
    monkeypatch.setattr(llm_service, "MAX_RETRIES", 3)
    monkeypatch.setattr(llm_service, "BASE_BACKOFF_SECONDS", 1.0)
    monkeypatch.setattr(llm_service, "MAX_RETRY_AFTER_SECONDS", 60.0)
    monkeypatch.setattr(llm_service, "RATE_LIMIT_FLOOR_SECONDS", 10.0)
    return SimpleNamespace(sleeps=sleeps, records=records, monkeypatch=monkeypatch)


def _service(provider="groq"):
    service = LLMService()
    service._configs["TESTROLE"] = AgentConfig("TESTROLE", provider, "test-model", "k")
    service._clients["TESTROLE"] = object()
    return service


def _script(service, outcomes, attr="_call_groq"):
    """Replaces the provider call; each call returns or raises the next outcome."""
    calls = []

    async def fake(*args, **kwargs):
        calls.append(1)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    setattr(service, attr, fake)
    return calls


def _run(service):
    return asyncio.run(service.call_agent("testrole", _MESSAGES))


def test_groq_client_has_sdk_retries_disabled():
    service = LLMService()
    client = service._client_for(AgentConfig("X", "groq", "m", "k"))
    assert client.max_retries == 0


def test_429_with_retry_after_within_budget_waits_then_succeeds(env):
    service = _service()
    calls = _script(service, [_rate_limit({"retry-after": "5"}), "ok"])

    assert _run(service) == "ok"

    assert len(calls) == 2
    assert env.sleeps == [5.0]
    record = env.records[-1]
    assert record["ok"] is True
    assert record["attempts"] == 2
    assert record["backoff_seconds"] == 5.0
    assert record["causes"] == ["rate_limit_429"]


def test_429_wait_is_at_least_requested_plus_bounded_jitter(env):
    env.monkeypatch.setattr(llm_service.random, "uniform", lambda low, high: high)
    service = _service()
    _script(service, [_rate_limit({"retry-after": "8"}), "ok"])

    _run(service)

    assert 8.0 <= env.sleeps[0] <= 8.0 * 1.25 + 1e-9


def test_retry_after_ms_is_preferred_over_retry_after(env):
    service = _service()
    _script(
        service,
        [_rate_limit({"retry-after-ms": "2500", "retry-after": "99"}), "ok"],
    )

    _run(service)

    assert env.sleeps == [2.5]


def test_429_over_budget_raises_without_retrying(env):
    service = _service()
    calls = _script(service, [_rate_limit({"retry-after": "120"}), "never used"])

    with pytest.raises(LLMServiceError, match="120s"):
        _run(service)

    assert len(calls) == 1
    assert env.sleeps == []
    record = env.records[-1]
    assert record["ok"] is False
    assert record["attempts"] == 1


def test_token_quota_style_long_wait_is_over_budget(env):
    service = _service()
    calls = _script(service, [_rate_limit({"retry-after": "412"}), "never used"])

    with pytest.raises(LLMServiceError, match="over the 60s budget"):
        _run(service)

    assert len(calls) == 1
    assert env.sleeps == []


def test_429_without_header_waits_at_least_the_floor(env):
    service = _service()
    _script(service, [_rate_limit(), "ok"])

    _run(service)

    assert env.sleeps == [10.0]


def test_429_with_unparseable_header_uses_the_floor(env):
    service = _service()
    _script(service, [_rate_limit({"retry-after": "7m12s"}), "ok"])

    _run(service)

    assert env.sleeps == [10.0]


def test_429_retries_exhausted_raises_after_max_retries(env):
    service = _service()
    calls = _script(service, [_rate_limit({"retry-after": "1"})] * 3)

    with pytest.raises(LLMServiceError, match="failed after 3 attempts"):
        _run(service)

    assert len(calls) == 3
    assert env.sleeps == [1.0, 2.0]
    assert env.records[-1]["ok"] is False
    assert env.records[-1]["attempts"] == 3


def test_non_429_client_error_fails_immediately(env):
    service = _service()
    calls = _script(service, [_status_error(400), "never used"])

    with pytest.raises(LLMServiceError, match="status 400"):
        _run(service)

    assert len(calls) == 1
    assert env.sleeps == []
    assert env.records[-1]["causes"] == ["http_400"]


def test_5xx_is_retried_with_backoff(env):
    service = _service()
    _script(service, [_status_error(503), "ok"])

    assert _run(service) == "ok"

    assert env.sleeps == [1.0]
    assert env.records[-1]["causes"] == ["http_503"]


def test_timeout_retries_exhaust_with_exponential_backoff(env):
    service = _service()
    calls = _script(service, [APITimeoutError(request=_REQUEST)] * 3)

    with pytest.raises(LLMServiceError, match="failed after 3 attempts"):
        _run(service)

    assert len(calls) == 3
    assert env.sleeps == [1.0, 2.0]


def test_connection_error_is_retried(env):
    service = _service()
    _script(service, [APIConnectionError(request=_REQUEST), "ok"])

    assert _run(service) == "ok"

    assert env.sleeps == [1.0]


def test_gemini_path_is_unchanged(env):
    service = _service(provider="gemini")
    calls = _script(service, [asyncio.TimeoutError(), "ok"], attr="_call_gemini")

    assert _run(service) == "ok"

    assert len(calls) == 2
    assert env.sleeps == [1.0]  # plain backoff, no 429 floor
    record = env.records[-1]
    assert record["provider"] == "gemini"
    assert record["causes"] == ["timeout"]


def test_429_log_shows_rate_limit_headers_and_hides_secrets(env, caplog):
    service = _service()
    _script(
        service,
        [
            _rate_limit(
                {
                    "retry-after": "3",
                    "x-ratelimit-remaining-tokens": "0",
                    "authorization": "Bearer secret-key",
                }
            ),
            "ok",
        ],
    )

    with caplog.at_level(logging.WARNING, logger="app.services.llm_service"):
        _run(service)

    text = caplog.text
    assert "status=429" in text
    assert "x-ratelimit-remaining-tokens" in text
    assert "secret-key" not in text


@pytest.mark.parametrize("value", ["-5", "nan", "inf", "abc", ""])
def test_unusable_retry_after_values_are_ignored(value):
    assert _retry_after_seconds(_rate_limit({"retry-after": value})) is None


def test_missing_response_gives_no_retry_after():
    assert _retry_after_seconds(Exception("no response")) is None


def test_safe_headers_redacts_sensitive_names():
    cleaned = _safe_headers({"Authorization": "Bearer x", "X-RateLimit-Limit": "10"})
    assert cleaned["authorization"] == "[redacted]"
    assert cleaned["x-ratelimit-limit"] == "10"