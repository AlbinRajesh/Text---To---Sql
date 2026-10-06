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
from typing import Any, Dict, List, Optional
from app.agent.prompts.grader_prompt import build_grader_prompt

logger = logging.getLogger(__name__)


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


async def grade_result(
    question: str,
    execution_result: Dict[str, Any],
    sql: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main entry point for Node 9.

    Args:
        question: the rewritten user question (from Node 1).
        execution_result: Node 8's output — {"results": [...], "columns": [...], "count": int}.
        sql: the validated SQL that produced the execution result, when available.

    Returns:
        {"is_valid": bool, "flagged": bool, "flag_reason": str | None}

    Raises ResultGraderError if the grading LLM call itself fails — caller
    treats this as "could not confirm correctness" and escalates, it does
    NOT assume the result is fine just because grading broke.
    """
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

    prompt = build_grader_prompt(question, results_text, sql)

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