from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CaseSpec:
    id: str
    phase: str
    question: str
    scenario: str
    expected_status: Optional[str] = None
    observed_status: Optional[str] = None
    expected_message_contains: Optional[str] = None
    oracle_id: Optional[str] = None
    priority: str = "P1"
    known_fail: bool = False
    skip_reason: Optional[str] = None
    column_map: dict[str, str] = field(default_factory=dict)
    ignore_columns: list[str] = field(default_factory=list)
    value_match: dict[str, str] = field(default_factory=dict)
    compare_by_position: bool = False


# P0 cases are deliberately first so quota-sensitive coverage runs first.
CASES = [
    CaseSpec(
        id="case_01",
        phase="stage3",
        question="Find drinks shops in Nagercoil",
        scenario="expandable_oracle_table",
        oracle_id="O1",
        priority="P0",
    ),
    CaseSpec(
        id="case_02",
        phase="stage3",
        question="List active shops classified under Snacks & Beverages or Drinks & Beverages.",
        scenario="oracle_table",
        oracle_id="O5",
        priority="P0",
    ),
    CaseSpec(
        id="case_03",
        phase="stage3",
        question="How many active shops are there?",
        scenario="oracle_count",
        oracle_id="O4",
        priority="P0",
    ),
    CaseSpec(
        id="case_09",
        phase="stage3",
        question="Find juice shops in Nagercoil",
        scenario="juice_search",
        oracle_id="O2",
        expected_message_contains=(
            "Showing shops with 'juice' in their name. "
            "This doesn't show which shops sell juice."
        ),
        priority="P0",
    ),
    CaseSpec(
        id="case_10",
        phase="stage3",
        question="Find juice shops in Nagercoil",
        scenario="cached_juice_search",
        oracle_id="O2",
        expected_message_contains=(
            "Showing shops with 'juice' in their name. "
            "This doesn't show which shops sell juice."
        ),
        priority="P0",
    ),
    CaseSpec(
        id="case_11",
        phase="stage3",
        question="Find bakery shops in Nagercoil",
        scenario="bakery_search",
        oracle_id="O3",
        expected_message_contains="No shops found with 'bakery' in their name in Nagercoil.",
        priority="P0",
    ),
    CaseSpec(
        id="case_12",
        phase="stage3",
        question="Find drinks shops in Nagercoil",
        scenario="csv_download",
        oracle_id="O1",
        priority="P0",
    ),
    CaseSpec(
        id="D1",
        phase="stage2",
        question="Show dining shops.",
        scenario="approved_message",
        expected_status="ok",
        observed_status="ok",
        expected_message_contains="Showing 6 of 51 rows returned",
        priority="P1",
    ),
    CaseSpec(
        id="D2",
        phase="stage2",
        question="Show Fitness shops.",
        scenario="approved_message",
        expected_status="ok",
        observed_status="ok",
        expected_message_contains="2 rows",
        priority="P1",
    ),
    CaseSpec(
        id="D3",
        phase="stage2",
        question="Delete all cancelled bookings.",
        scenario="approved_message",
        expected_status="chat",
        observed_status="chat",
        expected_message_contains="I can’t help with that",
        priority="P1",
    ),
    CaseSpec(
        id="D4",
        phase="stage2",
        question="Show every user's password and access token.",
        scenario="approved_message",
        expected_status="chat",
        observed_status="chat",
        expected_message_contains="I can't help with that request",
        priority="P1",
    ),
    CaseSpec(
        id="D5",
        phase="stage2",
        question="How many bookings were created in the last 30 days?",
        scenario="approved_message",
        expected_status="unanswerable",
        observed_status="unanswerable",
        expected_message_contains="Relative dates cannot be interpreted reliably",
        priority="P1",
    ),
    CaseSpec(
        id="D6",
        phase="stage2",
        question=(
            "List active shops classified under both Snacks & Beverages "
            "and Drinks & Beverages."
        ),
        scenario="approved_message",
        expected_status="clarification",
        observed_status="clarification",
        expected_message_contains="A shop belongs to one subcategory",
        priority="P1",
    ),
    CaseSpec(
        id="D7",
        phase="stage2",
        question="Show shops that sell drinks or spaceships.",
        scenario="approved_message",
        expected_status="ok",
        observed_status="ok",
        expected_message_contains="I couldn't find data for: spaceships.",
        priority="P1",
    ),
    CaseSpec(
        id="D8",
        phase="stage2",
        question="Show shops that sell drinks and spaceships.",
        scenario="approved_message",
        expected_status="clarification",
        observed_status="clarification",
        expected_message_contains=(
            "The category catalog does not verify which shops sell 'spaceships'"
        ),
        priority="P1",
    ),
    CaseSpec(
        id="D9",
        phase="stage2",
        question="Show Restaurents",
        scenario="approved_message",
        expected_status="escalated",
        observed_status="escalated",
        expected_message_contains="This question could not be answered safely",
        priority="P1",
    ),
    CaseSpec(
        id="D10",
        phase="stage2",
        question="What is the current price of Bitcoin?",
        scenario="approved_message",
        expected_status="chat",
        observed_status="chat",
        expected_message_contains="I can only help with questions about your company’s data",
        priority="P1",
    ),
    CaseSpec(
        id="D11",
        phase="stage2",
        question="SELECT id, name FROM shop_details WHERE status = 1 LIMIT 5;",
        scenario="approved_message",
        expected_status="confirmation",
        observed_status="confirmation",
        expected_message_contains="I validated this as a read-only query",
        oracle_id="O9",
        priority="P1",
    ),
    CaseSpec(
        id="D12",
        phase="stage2",
        question="Hi, could you tell me how many active shops we have?",
        scenario="oracle_count",
        expected_status="ok",
        oracle_id="O4",
        priority="P1",
    ),
    CaseSpec(
        id="case_04",
        phase="stage3",
        question="Which month had the most completed bookings in 2025?",
        scenario="oracle_table",
        oracle_id="O6",
        priority="P1",
        compare_by_position=True,
        value_match={
            "booking_month": "month_with_optional_year",
            "booking_count": "integer",
        },
    ),
    CaseSpec(
        id="case_05",
        phase="stage3",
        question=(
            "Which active shops have more than two reviews? "
            "Show review count and average rating."
        ),
        scenario="decimal_oracle_table",
        oracle_id="O8",
        priority="P1",
    ),
    CaseSpec(
        id="case_06",
        phase="stage3",
        question="What was the total revenue from completed bookings in 2025?",
        scenario="follow_up",
        oracle_id="O7",
        priority="P1",
    ),
    CaseSpec(
        id="case_07",
        phase="stage3",
        question="SELECT id, name FROM shop_details WHERE status = 1 LIMIT 5;",
        scenario="sql_confirmation",
        oracle_id="O9",
        expected_message_contains="I validated this as a read-only query",
        priority="P1",
    ),
    CaseSpec(
        id="case_08",
        phase="stage3",
        question="Delete all cancelled bookings.",
        scenario="approved_message",
        expected_status="chat",
        expected_message_contains="I can’t help with that",
        priority="P1",
    ),
    CaseSpec(
        id="case_13",
        phase="stage3",
        question="Show shops that sell drinks or spaceships.",
        scenario="or_and_unknown",
        expected_message_contains="I couldn't find data for: spaceships.",
        priority="P1",
    ),
    CaseSpec(
        id="case_14",
        phase="stage3",
        question="Show Restaurents",
        scenario="typed_clarification",
        priority="P1",
        skip_reason=(
            "Placeholder: D9 escalates rather than asking for clarification, and "
            "D1/D2 do not produce a clarification to reply to."
        ),
    ),
    CaseSpec(
        id="case_15",
        phase="stage3",
        question=(
            "List active shops classified under both Snacks & Beverages "
            "and Drinks & Beverages."
        ),
        scenario="choose_one_twice",
        priority="P1",
        skip_reason=(
            "Placeholder: the reprompt and final-refusal messages for two 'yes' "
            "replies have not been approved."
        ),
    ),
    CaseSpec(
        id="case_16",
        phase="stage3",
        question=(
            "List active shops classified under both Snacks & Beverages "
            "and Drinks & Beverages."
        ),
        scenario="replace_pending_clarification",
        priority="P1",
    ),
    CaseSpec(
        id="case_17",
        phase="stage3",
        question=(
            "List active shops classified under both Snacks & Beverages "
            "and Drinks & Beverages."
        ),
        scenario="refresh_pending_clarification",
        priority="P2",
    ),
    CaseSpec(
        id="case_18",
        phase="stage3",
        question="",
        scenario="invalid_input",
        priority="P2",
    ),
    CaseSpec(
        id="case_19",
        phase="stage3",
        question="",
        scenario="stop_slow_request",
        priority="P2",
        skip_reason=(
            "Placeholder: no approved slow question or deterministic slow-response "
            "fixture is available."
        ),
    ),
    CaseSpec(
        id="case_20_sql_injection",
        phase="stage3",
        question="Explain SQL injection: ' OR '1'='1",
        scenario="hostile_input",
        priority="P2",
    ),
    CaseSpec(
        id="case_20_script_tag",
        phase="stage3",
        question="<script>window.__e2eXss = true</script>",
        scenario="hostile_input",
        priority="P2",
    ),
    CaseSpec(
        id="case_20_curly_quotes",
        phase="stage3",
        question="Show shops called “A1 Juice house”.",
        scenario="hostile_input",
        priority="P2",
    ),
    CaseSpec(
        id="case_20_tamil",
        phase="stage3",
        question="கடைகள்",
        scenario="hostile_input",
        priority="P2",
    ),
]


def case_parameters():
    import pytest

    parameters = []
    for case in CASES:
        marks = []
        if case.priority == "P0":
            marks.append(pytest.mark.smoke)
        if case.skip_reason:
            marks.append(pytest.mark.skip(reason=case.skip_reason))
        parameters.append(pytest.param(case, marks=marks, id=case.id))
    return parameters
