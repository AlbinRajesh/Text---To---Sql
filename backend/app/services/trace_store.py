"""Persistent, request-correlated diagnostics for the agent pipeline."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def trace_directory() -> Path:
    configured = os.getenv("TEXT_TO_SQL_TRACE_DIR")
    if configured:
        return Path(configured)
    backend_root = Path(__file__).resolve().parents[2]
    return backend_root / "logs" / "pipeline_traces"


def persist_pipeline_trace(state: dict[str, Any]) -> Path | None:
    request_id = state.get("request_id")
    diagnostics = state.get("diagnostics")
    if not isinstance(request_id, str) or not isinstance(diagnostics, dict):
        logger.error("Cannot persist pipeline trace: request ID or diagnostics missing.")
        return None

    trace = {
        "request_id": request_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_question": state.get("user_question"),
        "classifier_result": {
            key: value
            for key, value in (state.get("classifier_result") or {}).items()
            if key in {"intent", "rewritten_question"}
        },
        "diagnostics": diagnostics,
        "timings": state.get("timings", []),
        "escalation_info": state.get("escalation_info"),
    }
    directory = trace_directory()
    path = directory / f"{request_id}.json"
    temporary_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{request_id}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            json.dump(trace, output, ensure_ascii=False, indent=2, default=str)
        temporary_path.replace(path)
    except OSError:
        logger.exception("Failed to persist pipeline trace for request_id=%s", request_id)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Failed to remove temporary pipeline trace for request_id=%s",
                    request_id,
                )
        return None
    return path
