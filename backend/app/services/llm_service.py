"""Configurable async LLM access for every AI-enabled agent role."""

import asyncio
import json
import logging
import math
import os
import random
import re
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
FAST_FALLBACK_TIMEOUT_SECONDS = float(
    os.getenv("LLM_FAST_FALLBACK_TIMEOUT_SECONDS", "30.0")
)
BACKUP_TIMEOUT_SECONDS = float(os.getenv("LLM_BACKUP_TIMEOUT_SECONDS", "30.0"))
CIRCUIT_BREAKER_FAILURE_THRESHOLD = int(
    os.getenv("LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
)
CIRCUIT_BREAKER_COOLDOWN_SECONDS = float(
    os.getenv("LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60.0")
)

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
        self._consecutive_provider_failures: Dict[tuple[str, str, str], int] = {}
        self._circuit_open_until: Dict[tuple[str, str, str], float] = {}

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

    def _client_for(
        self,
        config: AgentConfig,
        cache_key: str | None = None,
    ) -> Union[AsyncGroq, genai.Client]:
        key = cache_key or config.role
        client = self._clients.get(key)
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

        self._clients[key] = client
        return client

    @staticmethod
    def _fast_fallback_model(role: str) -> str | None:
        role_model = os.getenv(f"{role.upper()}_FAST_MODEL")
        model = role_model or os.getenv("LLM_FAST_FALLBACK_MODEL")
        return model.strip() if isinstance(model, str) and model.strip() else None

    @staticmethod
    def _endpoint_key(config: AgentConfig) -> tuple[str, str, str]:
        return config.role, config.provider, config.model

    @staticmethod
    def _backup_config(config: AgentConfig) -> AgentConfig | None:
        prefix = config.role.upper()
        model = os.getenv(f"{prefix}_BACKUP_MODEL") or os.getenv(
            "LLM_BACKUP_MODEL"
        )
        if not isinstance(model, str) or not model.strip():
            return None
        provider = (
            os.getenv(f"{prefix}_BACKUP_PROVIDER")
            or os.getenv("LLM_BACKUP_PROVIDER")
            or config.provider
        ).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise LLMConfigError(
                f"Unsupported backup provider '{provider}' for '{config.role}'."
            )
        api_key = (
            os.getenv(f"{prefix}_BACKUP_API_KEY")
            or os.getenv("LLM_BACKUP_API_KEY")
            or (config.api_key if provider == config.provider else None)
        )
        if not api_key:
            logger.warning(
                "Agent '%s' backup provider '%s' is configured without an API key; "
                "backup failover is disabled.",
                config.role,
                provider,
            )
            return None
        backup = AgentConfig(config.role, provider, model.strip(), api_key)
        if (backup.provider, backup.model) == (config.provider, config.model):
            return None
        return backup

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
        if max_tokens is None and role.strip().upper() == "QUERY_GENERATOR":
            max_tokens = 4096
        primary_config = self._config_for(role)
        backup_config = self._backup_config(primary_config)
        primary_endpoint_key = self._endpoint_key(primary_config)
        config = primary_config
        circuit_until = self._circuit_open_until.get(primary_endpoint_key, 0.0)
        if circuit_until > time.monotonic() and backup_config is not None:
            config = backup_config
            logger.warning(
                "Agent '%s' primary endpoint circuit is open; routing directly "
                "to the configured backup until cooldown expires.",
                config.role,
            )
        elif circuit_until and circuit_until <= time.monotonic():
            self._circuit_open_until.pop(primary_endpoint_key, None)
            self._consecutive_provider_failures.pop(primary_endpoint_key, None)
        client_key = (
            config.role
            if config == primary_config
            else f"{config.role}:{config.provider}:{config.model}"
        )
        client = self._client_for(config, cache_key=client_key)
        last_error: Optional[Exception] = None

        call_start = time.monotonic()
        causes: List[str] = []
        slept = 0.0
        last_infrastructure_code: Optional[str] = None
        provider_5xx_attempts = 0
        retry_after_over_budget: Optional[float] = None

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
                    result = await asyncio.wait_for(
                        self._call_groq(
                            config, client, messages, temperature, json_mode, max_tokens
                        ),
                        timeout=min(DEFAULT_TIMEOUT_SECONDS, 30.0),
                    )
                elif config.provider == "gemini":
                    result = await asyncio.wait_for(
                        self._call_gemini(
                            config, client, messages, temperature, json_mode, max_tokens
                        ),
                        timeout=min(DEFAULT_TIMEOUT_SECONDS, 30.0),
                    )
                else:
                    raise LLMConfigError(f"Unsupported provider '{config.provider}'.")
                if config == primary_config:
                    self._consecutive_provider_failures.pop(
                        primary_endpoint_key, None
                    )
                    self._circuit_open_until.pop(primary_endpoint_key, None)
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
                    retry_after_over_budget = server_wait
                    logger.warning(
                        "Agent '%s' retry-after exceeds the local retry budget; "
                        "trying the configured backup endpoint.",
                        role,
                    )
                    break
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
                    if rejected_text and _is_groq_safety_refusal(rejected_text):
                        causes.append("json_refused")
                        _record(False, attempt)
                        raise LLMRefusalError(
                            f"The '{role}' agent declined the request.",
                            refused_text=rejected_text,
                        ) from exc
                    cause = "json_empty" if not rejected_text else "json_format_error"
                    causes.append(cause)
                    if causes.count(cause) > 1:
                        _record(False, attempt)
                        error_type = (
                            LLMJSONFormatError
                            if rejected_text
                            else LLMServiceError
                        )
                        message = (
                            f"The '{role}' agent returned invalid JSON twice."
                            if rejected_text
                            else f"The '{role}' agent returned an empty reply twice."
                        )
                        raise error_type(message) from exc
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
                    if exc.status_code >= 500:
                        provider_5xx_attempts += 1
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
                if isinstance(status, int) and status >= 500:
                    provider_5xx_attempts += 1

            if json_mode and last_infrastructure_code == "LLM_TIMEOUT":
                logger.warning(
                    "Agent '%s' timed out; switching once to the configured "
                    "non-streaming JSON fallback.",
                    role,
                )
                break

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

        if json_mode and last_infrastructure_code == "LLM_TIMEOUT":
            fallback_model = self._fast_fallback_model(config.role)
            if fallback_model and fallback_model != config.model:
                fallback_config = AgentConfig(
                    config.role,
                    config.provider,
                    fallback_model,
                    config.api_key,
                )
                fallback_client = self._client_for(fallback_config)
                try:
                    if fallback_config.provider == "groq":
                        fallback_result = await asyncio.wait_for(
                            self._call_groq(
                                fallback_config,
                                fallback_client,
                                messages,
                                temperature,
                                True,
                                max_tokens,
                            ),
                            timeout=FAST_FALLBACK_TIMEOUT_SECONDS,
                        )
                    else:
                        fallback_result = await asyncio.wait_for(
                            self._call_gemini(
                                fallback_config,
                                fallback_client,
                                messages,
                                temperature,
                                True,
                                max_tokens,
                            ),
                            timeout=FAST_FALLBACK_TIMEOUT_SECONDS,
                        )
                    causes.append("fast_non_streaming_json_fallback")
                    _record(True, attempt + 1)
                    return fallback_result
                except Exception as exc:
                    causes.append("fast_json_fallback_failed")
                    last_error = exc
                    logger.warning(
                        "Agent '%s' fast JSON fallback model '%s' failed: %s",
                        role,
                        fallback_model,
                        exc,
                    )
            else:
                logger.warning(
                    "Agent '%s' fast JSON fallback was not run because no "
                    "different fallback model is configured.",
                    role,
                )

        if config == primary_config and provider_5xx_attempts:
            failures = self._consecutive_provider_failures.get(
                primary_endpoint_key, 0
            ) + provider_5xx_attempts
            self._consecutive_provider_failures[primary_endpoint_key] = failures
            if failures >= CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                open_until = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN_SECONDS
                self._circuit_open_until[primary_endpoint_key] = open_until
                logger.warning(
                    "Agent '%s' primary endpoint circuit opened for %.1f seconds "
                    "after repeated provider 5xx failures.",
                    role,
                    CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                )

        should_fail_over = last_infrastructure_code in {
            "LLM_RATE_LIMIT_429",
            "LLM_PROVIDER_5XX",
        }
        if (
            config == primary_config
            and should_fail_over
            and backup_config is not None
        ):
            backup_client_key = (
                backup_config.role
                if backup_config.provider == primary_config.provider
                else (
                    f"{backup_config.role}:"
                    f"{backup_config.provider}:{backup_config.model}"
                )
            )
            backup_client = self._client_for(
                backup_config,
                cache_key=backup_client_key,
            )
            try:
                if backup_config.provider == "groq":
                    fallback_result = await asyncio.wait_for(
                        self._call_groq(
                            backup_config,
                            backup_client,
                            messages,
                            temperature,
                            json_mode,
                            max_tokens,
                        ),
                        timeout=BACKUP_TIMEOUT_SECONDS,
                    )
                else:
                    fallback_result = await asyncio.wait_for(
                        self._call_gemini(
                            backup_config,
                            backup_client,
                            messages,
                            temperature,
                            json_mode,
                            max_tokens,
                        ),
                        timeout=BACKUP_TIMEOUT_SECONDS,
                    )
                causes.append("backup_provider_failover")
                _record(True, attempt + 1)
                return fallback_result
            except Exception as exc:
                causes.append("backup_provider_failover_failed")
                last_error = exc
                logger.warning(
                    "Agent '%s' backup endpoint '%s/%s' failed: %s",
                    role,
                    backup_config.provider,
                    backup_config.model,
                    exc,
                )

        if retry_after_over_budget is not None:
            _record(False, attempt)
            raise LLMInfrastructureError(
                f"Rate limit for the '{role}' agent asks for a "
                f"{retry_after_over_budget:.0f}s wait, which is over the "
                f"{MAX_RETRY_AFTER_SECONDS:.0f}s budget.",
                "LLM_RATE_LIMIT_429",
            ) from last_error

        _record(False, MAX_RETRIES)
        if causes and causes[-1] == "json_format_error":
            raise LLMJSONFormatError(
                f"Agent '{role}' returned invalid JSON after {MAX_RETRIES} attempts."
            ) from last_error
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
            repaired = _repair_truncated_json_object(raw)
            if repaired is None:
                raise LLMJSONFormatError(
                    f"Agent '{role}' returned invalid JSON: {raw!r}",
                    raw_output=raw,
                ) from exc
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as repair_exc:
                raise LLMJSONFormatError(
                    f"Agent '{role}' returned invalid JSON: {raw!r}",
                    raw_output=raw,
                ) from repair_exc
        if not isinstance(parsed, dict):
            raise LLMJSONFormatError(
                f"Agent '{role}' JSON response must be an object.",
                raw_output=raw,
            )
        return parsed, raw


def _repair_truncated_json_object(raw: str) -> Optional[str]:
    """Repair only trailing commas and missing closing object/array delimiters."""
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)

    stack: List[str] = []
    output: List[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            output.append(char)
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
            output.append(char)
        elif char in "}]":
            if not stack or stack.pop() != char:
                return None
            output.append(char)
        elif char == ",":
            next_nonspace = next(
                (candidate for candidate in text[index + 1:] if not candidate.isspace()),
                None,
            )
            if next_nonspace not in {"}", "]"}:
                output.append(char)
        else:
            output.append(char)

    if in_string or not output or not text.lstrip().startswith("{"):
        return None
    output.extend(reversed(stack))
    return "".join(output)

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
    The model returned clear refusal language. Never retried. `refused_text` is
    for logs only, never users.
    """

    def __init__(self, message: str, refused_text: str = "") -> None:
        super().__init__(message)
        self.refused_text = refused_text


class LLMJSONFormatError(LLMServiceError):
    """The model failed to return the requested JSON shape."""


def _is_groq_safety_refusal(text: str) -> bool:
    """Recognize a natural-language refusal without treating malformed JSON as one."""
    return bool(re.match(
        r"^\s*(?:"
        r"i\s+(?:cannot|can['’]t|will not|won['’]t|am unable to|must refuse)\b"
        r"|i(?:['’]m| am)\s+sorry\b.{0,80}\b"
        r"(?:cannot|can['’]t|will not|won['’]t|unable|refuse|"
        r"help with that|assist with that)\b"
        r"|sorry\b.{0,40}\bi\s+"
        r"(?:cannot|can['’]t|will not|won['’]t|am unable to|must refuse)\b"
        r")",
        text,
        re.IGNORECASE,
    ))


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
