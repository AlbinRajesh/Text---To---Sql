"""Prompt for Node 9 (result grader)."""

from typing import Optional

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
) -> str:
    sql_block = f"\n\nSQL that produced this result:\n{sql}" if sql else ""
    return (
        f"{GRADER_RULES}\n"
        f"Question: {question}"
        f"{sql_block}\n\n"
        f"Query result:\n{results_text}"
    )