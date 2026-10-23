"""Configurable async LLM access for every AI-enabled agent role."""

import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Union

from app.services.timing import record_llm_call
from app.services.langfuse_tracing import (
    finish_llm_generation,
    start_llm_generation,
)
from dotenv import load_dotenv
from groq import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncGroq,
    RateLimitError,
)
from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError as GeminiAPIError

load_dotenv()
logger = logging.getLogger(__name__)

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BASE_BACKOFF_SECONDS = float(os.getenv("LLM_BASE_BACKOFF_SECONDS", "1.0"))
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "30.0"))

# Longest wait we accept when the provider says "retry after N seconds".
# A longer request is treated as a service failure instead of retrying early.
MAX_RETRY_AFTER_SECONDS = float(os.getenv("LLM_MAX_RETRY_AFTER_SECONDS", "60"))
# Minimum wait after a 429 that carries no usable retry-after header.
RATE_LIMIT_FLOOR_SECONDS = float(os.getenv("LLM_RATE_LIMIT_FLOOR_SECONDS", "10"))
# Random extra wait (up to this fraction) so many callers do not retry together.
JITTER_FRACTION = 0.25

SUPPORTED_PROVIDERS = {"groq", "gemini"}

_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
}


class LLMServiceError(Exception):
    """Raised when an LLM call fails or returns invalid output."""

    def __init__(self, message: str, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class LLMInfrastructureError(LLMServiceError):
    """Raised when transient provider failures persist after bounded retries."""

    def __init__(self, message: str, infrastructure_code: str) -> None:
        super().__init__(message)
        self.infrastructure_code = infrastructure_code


class LLMConfigError(Exception):
    """Raised when a role's provider configuration is missing or unsupported."""


@dataclass(frozen=True)
class AgentConfig:
    role: str
    provider: str
    model: str
    api_key: str


def _with_jitter(seconds: float) -> float:
    """Adds up to JITTER_FRACTION of random extra time to a wait."""
    return seconds + random.uniform(0, seconds * JITTER_FRACTION)


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Reads the provider's requested wait (seconds) from a 429 response.

    Prefers 'retry-after-ms', then 'retry-after'. Returns None when the
    header is missing, unparseable, negative, or not a finite number.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            value = float(str(raw).strip()) * scale
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            return value
    return None


def _safe_headers(headers: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Returns response headers for logging with secrets redacted."""
    if headers is None:
        return {}
    try:
        return {
            str(key).lower(): (
                "[redacted]" if str(key).lower() in _SENSITIVE_HEADERS else str(value)
            )
            for key, value in headers.items()
        }
    except Exception:
        # Logging must never break an LLM call.
        return {}


def _usage_details(
    input_tokens: Any,
    output_tokens: Any,
    total_tokens: Any,
) -> Dict[str, int]:
    usage: Dict[str, int] = {}
    if isinstance(input_tokens, int) and input_tokens >= 0:
        usage["input"] = input_tokens
    if isinstance(output_tokens, int) and output_tokens >= 0:
        usage["output"] = output_tokens
    if isinstance(total_tokens, int) and total_tokens >= 0:
        usage["total"] = total_tokens
    elif "input" in usage and "output" in usage:
        usage["total"] = usage["input"] + usage["output"]
    return usage


class LLMService:
    """Routes each agent role to its independently configured provider/model."""

    def __init__(self) -> None:
        self._configs: Dict[str, AgentConfig] = {}
        self._clients: Dict[str, Union[AsyncGroq, genai.Client]] = {}

    def _config_for(self, role: str) -> AgentConfig:
        normalized_role = role.upper()
        config = self._configs.get(normalized_role)
        if config is not None:
            return config

        prefix = f"{normalized_role}_"
        provider = os.getenv(f"{prefix}PROVIDER", "groq").strip().lower()
        model = os.getenv(f"{prefix}MODEL")
        api_key = os.getenv(f"{prefix}API_KEY")

        if not model:
            raise LLMConfigError(
                f"{prefix}MODEL is not set for the '{role}' agent."
            )
        if not api_key:
            raise LLMConfigError(
                f"{prefix}API_KEY is not set for the '{role}' agent."
            )
        if provider not in SUPPORTED_PROVIDERS:
            raise LLMConfigError(
                f"Unsupported provider '{provider}' for '{role}'. "
                f"Supported providers: {sorted(SUPPORTED_PROVIDERS)}."
            )

        config = AgentConfig(normalized_role, provider, model, api_key)
        self._configs[normalized_role] = config
        return config

    def _client_for(self, config: AgentConfig) -> Union[AsyncGroq, genai.Client]:
        client = self._clients.get(config.role)
        if client is not None:
            return client

        if config.provider == "groq":
            # max_retries=0: call_agent is the ONLY retry owner, so every retry
            # is counted, logged, and honors the provider's retry-after header.
            client = AsyncGroq(
                api_key=config.api_key,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                max_retries=0,
            )
        elif config.provider == "gemini":
            client = genai.Client(api_key=config.api_key)
        else:
            # unreachable - _config_for already validated this
            raise LLMConfigError(f"Unsupported provider '{config.provider}'.")

        self._clients[config.role] = client
        return client

    async def _call_groq(
        self,
        config: AgentConfig,
        client: AsyncGroq,
        messages: List[Dict[str, str]],
        temperature: float,
        json_mode: bool,
        max_tokens: Optional[int],
    ) -> str:
        kwargs: Dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        observation = start_llm_generation(
            role=config.role,
            provider=config.provider,
            model=config.model,
            input_data=messages,
            temperature=temperature,
        )
        try:
            response = await client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            if not content:
                raise LLMServiceError(
                    f"Agent '{config.role}' returned empty content from model '{config.model}'."
                )
            usage = getattr(response, "usage", None)
            finish_llm_generation(
                observation,
                output=content,
                usage_details=_usage_details(
                    getattr(usage, "prompt_tokens", None),
                    getattr(usage, "completion_tokens", None),
                    getattr(usage, "total_tokens", None),
                ),
            )
            return content
        except BaseException as exc:
            finish_llm_generation(observation, error=exc)
            raise

    async def _call_gemini(
        self,
        config: AgentConfig,
        client: genai.Client,
        messages: List[Dict[str, str]],
        temperature: float,
        json_mode: bool,
        max_tokens: Optional[int],
    ) -> str:
        # Gemini has no "messages" list with roles the same way Groq/OpenAI
        # do for a single-turn call - flatten into one prompt string, since
        # every caller in this codebase sends a single user message anyway.
        prompt = "\n\n".join(m.get("content", "") for m in messages)

        config_kwargs: Dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            config_kwargs["max_output_tokens"] = max_tokens
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        generate_config = genai_types.GenerateContentConfig(**config_kwargs)

        observation = start_llm_generation(
            role=config.role,
            provider=config.provider,
            model=config.model,
            input_data=prompt,
            temperature=temperature,
        )
        try:
            response = await client.aio.models.generate_content(
                model=config.model,
                contents=prompt,
                config=generate_config,
            )
            content = response.text
            if not content:
                raise LLMServiceError(
                    f"Agent '{config.role}' returned empty content from model '{config.model}'."
                )
            usage = getattr(response, "usage_metadata", None)
            finish_llm_generation(
                observation,
                output=content,
                usage_details=_usage_details(
                    getattr(usage, "prompt_token_count", None),
                    getattr(usage, "candidates_token_count", None),
                    getattr(usage, "total_token_count", None),
                ),
            )
            return content
        except BaseException as exc:
            finish_llm_generation(observation, error=exc)
            raise

    def _log_rate_limit(
        self,
        role: str,
        attempt: int,
        exc: Exception,
        server_wait: Optional[float],
    ) -> None:
        """Logs enough of a 429 to tell requests-per-minute from tokens-per-minute."""
        response = getattr(exc, "response", None)
        logger.warning(
            "Agent '%s' rate limited (attempt %d/%d): status=%s "
            "retry_after_seconds=%s headers=%s message=%s",
            role,
            attempt,
            MAX_RETRIES,
            getattr(response, "status_code", "unknown"),
            server_wait,
            _safe_headers(getattr(response, "headers", None)),
            str(exc),
        )

    async def call_agent(
        self,
        role: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        config = self._config_for(role)
        client = self._client_for(config)
        last_error: Optional[Exception] = None

        call_start = time.monotonic()
        causes: List[str] = []
        slept = 0.0
        last_infrastructure_code: Optional[str] = None

        def _record(ok: bool, attempts: int) -> None:
            record_llm_call(
                role=config.role,
                provider=config.provider,
                model=config.model,
                seconds=time.monotonic() - call_start,
                attempts=attempts,
                causes=causes,
                backoff_seconds=slept,
                ok=ok,
            )

        for attempt in range(1, MAX_RETRIES + 1):
            rate_limited = False
            server_wait: Optional[float] = None
            try:
                if config.provider == "groq":
                    result = await self._call_groq(
                        config, client, messages, temperature, json_mode, max_tokens
                    )
                elif config.provider == "gemini":
                    result = await asyncio.wait_for(
                        self._call_gemini(
                            config, client, messages, temperature, json_mode, max_tokens
                        ),
                        timeout=DEFAULT_TIMEOUT_SECONDS + 5,
                    )
                else:
                    raise LLMConfigError(f"Unsupported provider '{config.provider}'.")
                _record(True, attempt)
                return result

            except LLMServiceError:
                _record(False, attempt)
                raise
            except RateLimitError as exc:
                last_error = exc
                last_infrastructure_code = "LLM_RATE_LIMIT_429"
                causes.append("rate_limit_429")
                rate_limited = True
                server_wait = _retry_after_seconds(exc)
                self._log_rate_limit(role, attempt, exc, server_wait)
                if server_wait is not None and server_wait > MAX_RETRY_AFTER_SECONDS:
                    # Retrying sooner than the provider asked would only fail
                    # again, so report a clear service failure instead.
                    _record(False, attempt)
                    raise LLMInfrastructureError(
                        f"Rate limit for the '{role}' agent asks for a "
                        f"{server_wait:.0f}s wait, which is over the "
                        f"{MAX_RETRY_AFTER_SECONDS:.0f}s budget.",
                        "LLM_RATE_LIMIT_429",
                    ) from exc
            except (APITimeoutError, APIConnectionError) as exc:
                last_error = exc
                last_infrastructure_code = (
                    "LLM_TIMEOUT" if isinstance(exc, APITimeoutError)
                    else "LLM_CONNECTION_ERROR"
                )
                causes.append("timeout_or_connection")
            except asyncio.TimeoutError as exc:
                last_error = exc
                last_infrastructure_code = "LLM_TIMEOUT"
                causes.append("timeout")
            except APIStatusError as exc:
                rejected_text = _groq_json_failure(exc)
                if rejected_text is not None:
                    # Groq's JSON check failed. Two very different cases.
                    if rejected_text:
                        # The model answered in plain words, so it declined.
                        # Retrying would repeat the same refusal.
                        causes.append("json_refused")
                        _record(False, attempt)
                        raise LLMRefusalError(
                            f"The '{role}' agent declined the request.",
                            refused_text=rejected_text,
                        ) from exc
                    # Empty reply: often temporary, so allow exactly one retry.
                    causes.append("json_empty")
                    if causes.count("json_empty") > 1:
                        _record(False, attempt)
                        raise LLMServiceError(
                            f"The '{role}' agent returned an empty reply twice."
                        ) from exc
                    last_error = exc
                else:
                    if 400 <= exc.status_code < 500 and exc.status_code != 429:
                        causes.append(f"http_{exc.status_code}")
                        _record(False, attempt)
                        raise LLMServiceError(
                            f"Groq rejected the '{role}' agent request "
                            f"(status {exc.status_code}): {exc}"
                        ) from exc
                    last_error = exc
                    causes.append(f"http_{exc.status_code}")
                    last_infrastructure_code = (
                        "LLM_PROVIDER_5XX"
                        if exc.status_code >= 500
                        else None
                    )
            except GeminiAPIError as exc:
                status = getattr(exc, "code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    causes.append(f"gemini_{status}")
                    _record(False, attempt)
                    raise LLMServiceError(
                        f"Gemini rejected the '{role}' agent request "
                        f"(status {status}): {exc}"
                    ) from exc
                last_error = exc
                causes.append(f"gemini_{status}")
                last_infrastructure_code = (
                    "LLM_RATE_LIMIT_429"
                    if status == 429
                    else "LLM_PROVIDER_5XX"
                    if isinstance(status, int) and status >= 500
                    else None
                )

            logger.warning(
                "Agent '%s' failed (attempt %d/%d): %s",
                role,
                attempt,
                MAX_RETRIES,
                repr(last_error),
            )
            if attempt < MAX_RETRIES:
                delay = _with_jitter(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                if rate_limited:
                    # Wait at least what the provider asked; with no usable
                    # header, wait at least the configured floor.
                    requested = (
                        server_wait
                        if server_wait is not None
                        else RATE_LIMIT_FLOOR_SECONDS
                    )
                    delay = max(delay, _with_jitter(requested))
                slept += delay
                await asyncio.sleep(delay)

        _record(False, MAX_RETRIES)
        message = f"Agent '{role}' failed after {MAX_RETRIES} attempts: {last_error}"
        if last_infrastructure_code:
            raise LLMInfrastructureError(message, last_infrastructure_code) from last_error
        raise LLMServiceError(message) from last_error

    async def call_agent_json(
        self,
        role: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        parsed, _ = await self.call_agent_json_with_raw(
            role,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return parsed

    async def call_agent_json_with_raw(
        self,
        role: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> tuple[Dict[str, Any], str]:
        """Return a validated JSON object together with the provider's raw text."""
        raw = await self.call_agent(
            role,
            messages,
            temperature=temperature,
            json_mode=True,
            max_tokens=max_tokens,
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMServiceError(
                f"Agent '{role}' returned invalid JSON: {raw!r}",
                raw_output=raw,
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMServiceError(f"Agent '{role}' JSON response must be an object.")
        return parsed, raw

    async def call_cheap_model_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Compatibility wrapper for the classifier while callers migrate."""
        return await self.call_agent_json(
            "CLASSIFIER", messages, temperature, max_tokens
        )

    async def call_strong_model_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Compatibility wrapper for query generation while callers migrate."""
        return await self.call_agent_json(
            "QUERY_GENERATOR", messages, temperature, max_tokens
        )


_llm_service_instance: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_service_instance
    if _llm_service_instance is None:
        _llm_service_instance = LLMService()
    return _llm_service_instance


class LLMRefusalError(LLMServiceError):
    """
    The model answered in plain words instead of JSON, which means it declined
    the request. Never retried. `refused_text` is for logs only, never users.
    """

    def __init__(self, message: str, refused_text: str = "") -> None:
        super().__init__(message)
        self.refused_text = refused_text


def _groq_json_failure(exc: Exception):
    """
    Returns None if `exc` is not a Groq "json_validate_failed" error.
    Otherwise returns the text the model produced ("" when it was empty).
    """
    import re

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("code") == "json_validate_failed":
            return str(err.get("failed_generation") or "").strip()

    text = str(exc)
    if "json_validate_failed" not in text:
        return None
    match = re.search(
        r"'failed_generation':\s*(['\"])(.*)\1\s*\}", text, re.DOTALL
    )
    return match.group(2).strip() if match else ""
