"""
result_grader.py  (Node 9: Result Grader)

Final check before an answer reaches the user or gets cached: does the
executed query's result actually answer the original question?

This catches a class of bug the earlier nodes can't: everything can be
"valid" (safe SQL, real table, real columns, query runs without error)
while still being the WRONG query — e.g. filtering on the wrong status
column, or matching too broadly. This node is the last line of defense
before Node 10 (return + cache).

BLOCKING by design — runs before the user sees the answer, per the locked
pipeline plan (Node 9 -> Node 10, in that order). If the grade fails, the
caller routes to Node 11 (human escalation), same as a Node 7 safety
failure — an answer that fails this check is never shown to the user and
never cached.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time, timezone

from app.agent.prompts.grader_prompt import build_grader_prompt

logger = logging.getLogger(__name__)

# Restrict string dates to ISO syntax so ordinary text never becomes orderable.
_ISO_DATE_VALUE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_VALUE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)


def _normalize_ranked_metric(value: Any) -> Optional[tuple[str, Any]]:
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, date):
        current = datetime.combine(value, time.min)
    elif isinstance(value, str) and _ISO_DATETIME_VALUE_PATTERN.fullmatch(value):
        try:
            current = datetime.fromisoformat(
                value[:-1] + "+00:00" if value[-1:].casefold() == "z" else value
            )
        except ValueError:
            return None
    elif isinstance(value, str) and _ISO_DATE_VALUE_PATTERN.fullmatch(value):
        try:
            current = datetime.combine(date.fromisoformat(value), time.min)
        except ValueError:
            return None
    else:
        if isinstance(value, bool):
            return None
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return "numeric", numeric

    try:
        # Naive timestamps remain wall-clock values; aware ones compare in UTC.
        if current.utcoffset() is not None:
            current = current.astimezone(timezone.utc).replace(tzinfo=None)
    except (OverflowError, TypeError, ValueError):
        return None
    return "date", current


class ResultGraderError(Exception):
    """
    Raised when the LLM grading call itself fails (not when the grade is
    simply "no") — e.g. API error, malformed JSON. Caller should treat
    this the same as a failed grade and route to Node 11, since we cannot
    confirm the answer is correct.
    """
    pass


def _format_results_for_prompt(execution_result: Dict[str, Any], max_rows: int = 20) -> str:
    """
    Compact text preview of the result set for the grading prompt.
    Capped at max_rows — grading needs to see the SHAPE of the answer
    (right columns, plausible values), not every row of a 200-row result.
    """
    results = execution_result.get("results", [])
    columns = execution_result.get("columns", [])
    count = execution_result.get("count", 0)
    truncated = bool(execution_result.get("truncated", False))

    if count == 0:
        return "Query returned 0 rows."

    preview_rows = results[:max_rows]
    lines = [f"Columns: {columns}", f"Rows returned: {count}"]
    if truncated:
        lines.append(
            "WARNING: The result is truncated at the configured row limit; "
            "the returned row count is not the full matching-row total."
        )
    lines.append("Sample rows:")
    for row in preview_rows:
        lines.append(str(row))
    if count > max_rows:
        lines.append(f"... ({count - max_rows} more rows not shown)")
    return "\n".join(lines)


def _ranked_result_violation(
    execution_result: Dict[str, Any],
    ranking: Optional[Dict[str, Any]],
) -> Optional[str]:
    if ranking is None:
        return None
    metric_column = ranking.get("column")
    direction = ranking.get("direction")
    columns = execution_result.get("columns", [])
    rows = execution_result.get("results", [])
    if (
        not isinstance(metric_column, str)
        or direction not in {"ASC", "DESC"}
        or not isinstance(columns, list)
        or metric_column not in columns
        or not isinstance(rows, list)
    ):
        return "The ranked result is missing its validated ranking metric."

    previous = None
    previous_is_null = False
    has_previous = False
    metric_kind = None
    for row in rows:
        if not isinstance(row, dict) or metric_column not in row:
            return "The ranked result is missing a metric value."
        value = row[metric_column]
        if value is None:
            current = None
            current_is_null = True
        else:
            normalized = _normalize_ranked_metric(value)
            if normalized is None:
                return "The ranked metric contains a non-numeric value."
            current_kind, current = normalized
            if metric_kind is not None and current_kind != metric_kind:
                return "The ranked metric contains incompatible value types."
            if current_kind == "numeric" and not current.is_finite():
                return "The ranked metric contains a non-finite value."
            metric_kind = current_kind
            current_is_null = False

        if has_previous:
            if direction == "DESC" and (
                (previous_is_null and not current_is_null)
                or (
                    not previous_is_null
                    and not current_is_null
                    and previous < current
                )
            ):
                return "Rows are not ordered by the ranked metric descending."
            if direction == "ASC" and (
                (not previous_is_null and current_is_null)
                or (
                    not previous_is_null
                    and not current_is_null
                    and previous > current
                )
            ):
                return "Rows are not ordered by the ranked metric ascending."
        previous, previous_is_null = current, current_is_null
        has_previous = True
    return None


def _verified_tied_leaders(
    execution_result: Dict[str, Any],
    ranking: Optional[Dict[str, Any]],
) -> bool:
    """Recognize complete tie expansion for a validated ranked query."""
    if (
        execution_result.get("tie_at_limit") is not True
        or execution_result.get("truncated") is True
        or ranking is None
    ):
        return False

    metric_column = ranking.get("column")
    columns = execution_result.get("columns", [])
    rows = execution_result.get("results", [])
    if (
        not isinstance(metric_column, str)
        or ranking.get("direction") not in {"ASC", "DESC"}
        or not isinstance(columns, list)
        or metric_column not in columns
        or not isinstance(rows, list)
        or len(rows) < 2
    ):
        return False

    try:
        values = [Decimal(str(row[metric_column])) for row in rows]
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return False
    return all(value.is_finite() for value in values) and all(
        value == values[0] for value in values[1:]
    )


async def grade_result(
    question: str,
    execution_result: Dict[str, Any],
    sql: Optional[str] = None,
    context_note: Optional[str] = None,
    ranking: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Main entry point for Node 9.

    Args:
        question: the rewritten user question (from Node 1).
        execution_result: Node 8's output — {"results": [...], "columns": [...], "count": int}.
        sql: the validated SQL that produced the execution result, when available.
        context_note: a note written by our own code (never user text) that lists
            filters applied before the SQL ran, so the grader can see them.

    Returns:
        {"is_valid": bool, "flagged": bool, "flag_reason": str | None}

    Raises ResultGraderError if the grading LLM call itself fails — caller
    treats this as "could not confirm correctness" and escalates, it does
    NOT assume the result is fine just because grading broke.
    """
    ranking_violation = _ranked_result_violation(execution_result, ranking)
    if ranking_violation is not None:
        logger.warning(
            "Node 9 rejected ranked result for question=%r: %s",
            question,
            ranking_violation,
        )
        return {
            "is_valid": False,
            "flagged": True,
            "flag_reason": ranking_violation,
        }

    if _verified_tied_leaders(execution_result, ranking):
        logger.info(
            "Node 9: accepting complete tied leaders for question=%r",
            question,
        )
        return {
            "is_valid": True,
            "flagged": False,
            "flag_reason": None,
        }

    no_data_reason = execution_result.get("no_data_reason")
    if no_data_reason in {"no_rows", "all_null"}:
        logger.info(
            "Node 9: accepting verified empty aggregate outcome (%s) for question=%r",
            no_data_reason,
            question,
        )
        return {
            "is_valid": True,
            "flagged": False,
            "flag_reason": None,
        }

    from app.services.llm_service import get_llm_service

    # Zero rows is a real, common, VALID answer (e.g. "how many users
    # signed up today" -> 0 is correct if nobody did). Don't auto-flag it
    # here — let the LLM judge whether zero rows makes sense for this
    # specific question, same as any other row count.
    results_text = _format_results_for_prompt(execution_result)

    prompt = build_grader_prompt(question, results_text, sql, context_note)

    llm = get_llm_service()
    try:
        parsed = await llm.call_agent_json(
            "RESULT_GRADER", [{"role": "user", "content": prompt}], temperature=0.0
        )
    except Exception as e:
        logger.error("Node 9 grading call failed: %s", e)
        raise ResultGraderError(f"Result grading failed: {e}") from e

    is_valid = bool(parsed.get("is_valid", False))
    flag_reason = parsed.get("flag_reason")

    if is_valid:
        logger.info("Node 9: result graded VALID for question=%r", question)
    else:
        logger.warning(
            "Node 9: result graded INVALID for question=%r. Reason: %s",
            question, flag_reason,
        )

    return {
        "is_valid": is_valid,
        "flagged": not is_valid,
        "flag_reason": flag_reason,
    }