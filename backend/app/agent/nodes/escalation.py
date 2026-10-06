"""
escalation.py  (Node 11: Human-in-the-Loop Escalation)

Terminal node for anything the pipeline could not safely handle: a schema
grading failure, a safety violation, an execution failure, an invalid
result grade, or a classifier failure. Writes a permanent audit record
so a human can review WHY a real user's question failed, then returns a
generic, safe message to the user.

Deliberately does NOT expose internal error details (SQL, stack traces,
schema info) in the user-facing message — only in the logged audit record.
Leaking internals in an error message shown to end users is both a
security risk (schema/query structure disclosure) and unhelpful to them.
"""

import json
import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

# JSON-lines audit log — one escalation per line, append-only. Swap for a
# real database table (e.g. an `escalations` table) once one exists; this
# is a minimal persistent record so escalations survive past the terminal
# session, which the previous version (a plain logger.warning) did not.
AUDIT_LOG_PATH = os.getenv("ESCALATION_AUDIT_LOG_PATH", "logs/escalations.jsonl")

USER_FACING_MESSAGE = (
    "This question could not be answered safely. It has been logged for review."
)


def _write_audit_record(record: Dict[str, Any]) -> None:
    """
    Appends one escalation record to the audit log. Failure to write the
    audit log must NEVER crash the pipeline or block returning a response
    to the user — logging the failure is the most we can safely do here.
    """
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG_PATH) or ".", exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as e:
        logger.error("Failed to write escalation audit record to disk: %s", e)


async def escalate(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry point for Node 11.

    Args:
        state: the current GraphState dict. Reads (but does not mutate)
            escalation_info, request_id, user_question, and the rewritten
            question from classifier_result if available.

    Returns:
        {"final_answer": dict}
        Caller (graph.py) is responsible for writing this back into state.
    """
    info = state.get("escalation_info", {})

    record = {
        "timestamp": time.time(),
        "request_id": state.get("request_id"),
        "user_question": state.get("user_question"),
        "rewritten_question": (state.get("classifier_result") or {}).get("rewritten_question"),
        "node": info.get("node"),
        "reason": info.get("reason"),
        "attempts_made": info.get("attempts_made"),
    }

    logger.warning("ESCALATION: %s", record)
    _write_audit_record(record)

    return {
        "final_answer": {
            "answer": None,
            "error": USER_FACING_MESSAGE,
        }
    }