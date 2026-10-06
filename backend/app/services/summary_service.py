"""Create concise summaries from values already present in query results."""

import re
from typing import Any, Dict, Optional

_SENSITIVE_COLUMN = re.compile(
    r"phone|mobile|e?mail|address|password|otp|token|secret|"
    r"licen[cs]e|insurance|rcbook|vechicle",
    re.IGNORECASE,
)
MAX_SUMMARY_CHARS = 300


def _single_value_label(answer: Dict[str, Any]) -> Optional[str]:
    labels = answer.get("column_labels") or {}
    results = answer.get("results") or []
    columns = answer.get("columns") or []

    if results and isinstance(results[0], dict) and len(results[0]) == 1:
        column = next(iter(results[0]))
    elif len(columns) == 1:
        column = columns[0]
    else:
        return None

    if _SENSITIVE_COLUMN.search(str(column)):
        return None
    return str(labels.get(column) or str(column).replace("_", " ").capitalize())


async def generate_summary(
    original_question: str, answer: Dict[str, Any]
) -> Optional[str]:
    """Summarize only returned scalar values or the number of returned rows."""
    del original_question
    if answer.get("truncated"):
        return None

    answer_type = answer.get("answer_type")
    results = answer.get("results") or []
    if answer_type == "single_value":
        label = _single_value_label(answer)
        value = answer.get("single_value")
        if label is None or value is None or isinstance(value, (dict, list, tuple)):
            return None
        summary = f"{label}: {value}."
        return summary if len(summary) <= MAX_SUMMARY_CHARS else None

    if answer_type == "table":
        count = len(results)
        noun = "row" if count == 1 else "rows"
        return f"Returned {count} {noun}."

    return None
