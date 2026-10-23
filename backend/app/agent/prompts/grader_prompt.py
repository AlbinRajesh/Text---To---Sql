"""Prompt for Node 9 (result grader)."""

from typing import Any, Dict, Optional

GRADER_RULES = """\
You check whether a database result answers the user's question.
The SQL was already verified as safe, correct and structurally valid. Do NOT
judge the SQL. Judge only whether the returned rows contradict the question.

Answer is_valid=true in ALL of these cases:
1. The question asks for ONE item ("which", "highest", "lowest", "top 1")
   and one row came back. One row is the expected answer.
2. The question asks for "top N" and fewer than N rows came back. The data
   may simply have fewer than N groups. Never reject for this reason.
3. A requested column is present but its value is empty, null, "-" or 0.
   A missing value in the database is not a wrong answer.
4. The result is a single count, sum or average, and the number is
   plausible (not negative for a count).
5. The question's filters (category, city, offer name, number of offers)
   are not shown in the result columns. Filters are applied inside the SQL
   and never appear in the output. Do not ask for "evidence" of them.
6. The result has 0 rows. Zero rows is a valid answer.
7. The result is truncated at the row limit. Judge the shape, not the total.
8. The Trusted system note says a ranked list was returned deliberately.
   Then several rows are valid, even if the question sounds like one item
   ("the shop with the highest rating"). Rule 1 does not apply.
9. A Trusted ranked-result note gives the metric, direction, and limit.
    Later rows in a correct top-N list may have lower values. Check that the
    displayed metric values follow the stated direction; reject a list whose
    values are out of order, but do not reject merely because later ranks are
    lower than the first.

Answer is_valid=false ONLY when one of these is true:
A. A column the user asked for is completely absent from the result columns.
B. The values clearly contradict the question (for example a count that is
   negative, a rating above the possible maximum, text where a number was
   requested, or an answer about a different thing than the one asked).
C. The result shape cannot answer the question (for example the question
   asks for a list of names and only numbers came back).

When unsure, answer is_valid=true. Rejecting a correct answer is worse than
accepting an answer with a missing value.

Respond ONLY with JSON:
{"is_valid": true/false, "flag_reason": "short reason if not valid, else null"}
"""


def build_grader_prompt(
    question: str,
    results_text: str,
    sql: Optional[str] = None,
    context_note: Optional[str] = None,
) -> str:
    sql_block = f"\n\nSQL that produced this result:\n{sql}" if sql else ""
    note_block = (
        f"\n\nTrusted system note (written by the system, not the user):\n{context_note}"
        if context_note
        else ""
    )
    return (
        f"{GRADER_RULES}\n"
        f"Question: {question}"
        f"{sql_block}"
        f"{note_block}\n\n"
        f"Query result:\n{results_text}"
    )


def ranked_result_metadata(
    validated_plan: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Derive grading metadata only from the already validated query plan."""
    limit = validated_plan.get("limit")
    order_by = validated_plan.get("order_by")
    if type(limit) is not int or limit < 1 or not isinstance(order_by, list):
        return None
    if not order_by or not isinstance(order_by[0], dict):
        return None

    order = order_by[0]
    alias = order.get("column")
    direction = order.get("direction")
    if not isinstance(alias, str) or direction not in {"ASC", "DESC"}:
        return None
    selected = next(
        (
            item for item in validated_plan.get("select_items", [])
            if isinstance(item, dict) and item.get("alias") == alias
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                item
                for aggregate in validated_plan.get("isolated_child_aggregates", [])
                if isinstance(aggregate, dict)
                for item in aggregate.get("select_items", [])
                if isinstance(item, dict) and item.get("alias") == alias
            ),
            None,
        )
    if selected is None:
        return None
    metric = selected.get("metric") or selected.get("alias")
    return {
        "metric": metric,
        "column": alias,
        "direction": direction,
        "limit": limit,
    }


def build_ranked_result_note(metadata: Dict[str, Any]) -> str:
    """Render compact trusted ranking facts; every value comes from validation."""
    return (
        "A ranked, limited list was returned deliberately. "
        f"Ranking metric: {metadata['metric']}; "
        f"direction: {metadata['direction']}; "
        f"limit: {metadata['limit']}. "
        "Lower values in later ranks are expected for descending rankings; "
        "verify that rows follow the stated direction."
    )