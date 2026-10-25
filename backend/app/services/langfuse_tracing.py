"""Optional Langfuse tracing helpers for graph and direct provider calls."""

import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from langfuse import get_client
from langfuse.langchain import CallbackHandler

load_dotenv()
logger = logging.getLogger(__name__)

_configured_warning_logged = False


def langfuse_configured() -> bool:
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    )


def create_callback_handler() -> Optional[CallbackHandler]:
    global _configured_warning_logged
    if not langfuse_configured():
        if not _configured_warning_logged:
            logger.warning(
                "Langfuse tracing is disabled because its credentials are not configured."
            )
            _configured_warning_logged = True
        return None
    try:
        return CallbackHandler()
    except Exception:
        logger.exception("Could not initialize the Langfuse graph callback handler.")
        return None


def start_llm_generation(
    *,
    role: str,
    provider: str,
    model: str,
    input_data: Any,
    temperature: float,
) -> Any:
    if not langfuse_configured():
        return None
    try:
        return get_client().start_observation(
            name=f"{provider}.{role.lower()}",
            as_type="generation",
            input=input_data,
            model=model,
            model_parameters={"temperature": temperature},
            metadata={"agent_role": role, "provider": provider},
        )
    except Exception:
        logger.exception("Could not start Langfuse generation observation.")
        return None


def finish_llm_generation(
    observation: Any,
    *,
    output: Optional[str] = None,
    usage_details: Optional[dict[str, int]] = None,
    error: Optional[BaseException] = None,
) -> None:
    if observation is None:
        return
    try:
        update: dict[str, Any] = {}
        if output is not None:
            update["output"] = output
        if usage_details:
            update["usage_details"] = usage_details
        if error is not None:
            update["level"] = "ERROR"
            update["status_message"] = type(error).__name__
        try:
            observation.update(**update)
        finally:
            observation.end()
    except Exception:
        logger.exception("Could not finish Langfuse generation observation.")
