"""Golden Benchmark Catalog 4 (Cases 66-72).

Focused regressions for failures observed in the latest live chat run.
Executable cases use the master benchmark runner's SQL and conversation
paths. Frontend-only flows that the runner cannot exercise are documented in
``MANUAL_DIAGNOSTIC_SCENARIOS`` with the evidence needed to identify their
root cause.
"""

from typing import Any, Dict, List, Sequence


def _case(
    number: int,
    category: str,
    question: str,
    golden_tables: Sequence[str],
    golden_sql: str,
    *,
    note: str,
    case_type: str = "sql",
    turns: Sequence[Dict[str, str]] = (),
) -> Dict[str, Any]:
    return {
        "number": number,
        "category": category,
        "question": question,
        "golden_tables": list(golden_tables),
        "golden_sql": golden_sql.strip(),
        "expected_outcome": "sql",
        "note": note,
        "case_type": case_type,
        "expected_intent": "",
        "expected_complexity": "",
        "expected_route": "",
        "turns": [dict(turn) for turn in turns],
        "expected_clarification": False,
    }


GOLDEN_BENCHMARK_CASES: List[Dict[str, Any]] = [
    _case(
        66,
        "tie_handling",
        "Which month had the most completed bookings in 2025?",
        ["bookings"],
        "SELECT time_slot_month AS booking_month, COUNT(*) AS booking_count "
        "FROM bookings WHERE status = 1 "
        "AND time_slot_date >= '2025-01-01' AND time_slot_date < '2026-01-01' "
        "GROUP BY time_slot_month "
        "HAVING COUNT(*) = ("
        "SELECT MAX(monthly.booking_count) FROM ("
        "SELECT COUNT(*) AS booking_count FROM bookings WHERE status = 1 "
        "AND time_slot_date >= '2025-01-01' AND time_slot_date < '2026-01-01' "
        "GROUP BY time_slot_month"
        ") AS monthly"
        ") ORDER BY booking_month;",
        case_type="conversation",
        turns=[{
            "question": "Which month had the most completed bookings in 2025?",
            "expected": "answer",
        }],
        note=(
            "Graph-level regression: accept every month tied at the maximum. "
            "Expected May and November, 8 bookings each; a tie rejection should "
            "be reported at conversation_turn_1."
        ),
    ),
    _case(
        67,
        "address_validation",
        "List subcategories in Nagercoil having average offer price below 500.",
        ["subcategories", "shop_details", "offer", "shop_address"],
        "SELECT sc.subcategoryname "
        "FROM subcategories sc "
        "JOIN shop_details s ON sc.id = s.subcategory_id "
        "JOIN offer o ON s.id = o.shop_id "
        "JOIN shop_address a ON a.shop_id = s.id "
        "WHERE s.status = 1 AND a.city = 'Nagercoil' AND a.is_default = 1 "
        "GROUP BY sc.id, sc.subcategoryname "
        "HAVING AVG(o.offer_price) < 500;",
        note=(
            "Validator/generator regression: the default-address predicate is "
            "required and must be accepted when emitted in the join plan."
        ),
    ),
    _case(
        68,
        "address_validation",
        "Categories and the count of unique cities where their shops operate.",
        ["categories", "shop_details", "shop_address"],
        "SELECT c.categoriesname, COUNT(DISTINCT a.city) AS unique_cities "
        "FROM categories c "
        "JOIN shop_details s ON c.id = s.category_id "
        "JOIN shop_address a ON a.shop_id = s.id "
        "WHERE s.status = 1 AND a.is_default = 1 "
        "GROUP BY c.id, c.categoriesname;",
        note=(
            "Validator/generator regression: COUNT(DISTINCT city) with "
            "shop_address.is_default = 1 must not be rejected."
        ),
    ),
    _case(
        69,
        "clarification_routing",
        "Which categories currently have no active shops?",
        ["categories", "shop_details"],
        "SELECT c.categoriesname AS category_name "
        "FROM categories c "
        "LEFT JOIN shop_details s "
        "ON s.category_id = c.id AND s.status = 1 "
        "GROUP BY c.id, c.categoriesname "
        "HAVING COUNT(s.id) = 0 "
        "ORDER BY c.categoriesname;",
        case_type="conversation",
        turns=[{
            "question": "Which categories currently have no active shops?",
            "expected": "answer",
        }],
        note=(
            "The complete data question must be answered, not sent to "
            "clarification; the observed UI also needs a manual yes-reply "
            "reproduction listed in MANUAL_DIAGNOSTIC_SCENARIOS."
        ),
    ),
    _case(
        70,
        "clarification_routing",
        "How much revenue did each subscription plan generate from paid invoices?",
        ["invoices", "plans"],
        "SELECT p.name, SUM(i.final_amount) AS revenue "
        "FROM invoices i JOIN plans p ON p.id = i.plan_id "
        "WHERE i.status = 'paid' "
        "GROUP BY p.id, p.name;",
        note=(
            "A request for each plan is a breakdown and should not ask the user "
            "to choose one plan."
        ),
    ),
    _case(
        71,
        "conversation_follow_up",
        "Which category generated the most completed booking revenue? Then: "
        "Show the shops in that category, ranked by revenue.",
        ["bookings", "shop_details", "categories"],
        "SELECT s.name AS shop_name, SUM(b.final_amount) AS total_revenue "
        "FROM bookings b "
        "JOIN shop_details s ON s.id = b.shop_id "
        "JOIN categories c ON c.id = s.category_id "
        "WHERE b.status = 1 AND c.categoriesname = 'Services' "
        "GROUP BY s.id, s.name "
        "ORDER BY total_revenue DESC;",
        case_type="conversation",
        turns=[
            {
                "question": "Which category generated the most completed booking revenue?",
                "expected": "answer",
            },
            {"question": "Show the shops in that category, ranked by revenue."},
        ],
        note=(
            "The follow-up must retain the winning category (Services) and "
            "completed-booking revenue metric."
        ),
    ),
    _case(
        72,
        "conversation_context",
        "Which category generated the most completed booking revenue? Then: "
        "Show the shops in that category, ranked by revenue.",
        ["bookings", "shop_details", "categories", "reviews"],
        "SELECT s.name AS shop_name, SUM(b.final_amount) AS total_revenue "
        "FROM bookings b "
        "JOIN shop_details s ON s.id = b.shop_id "
        "JOIN categories c ON c.id = s.category_id "
        "WHERE b.status = 1 AND c.categoriesname = 'Services' "
        "GROUP BY s.id, s.name "
        "ORDER BY total_revenue DESC;",
        case_type="conversation",
        turns=[
            {
                "question": (
                    "Which active shops have more than two reviews? "
                    "Show review count and average rating."
                ),
                "expected": "answer",
            },
            {
                "question": (
                    "Which category generated the most completed booking "
                    "revenue? Then: Show the shops in that category, ranked "
                    "by revenue."
                ),
            },
        ],
        note=(
            "Run unrelated review context before the category drill-down. "
            "The final result must be the shops in the highest-revenue "
            "completed-booking category, not review_count or average_rating."
        ),
    ),
]


MANUAL_DIAGNOSTIC_SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "clarification-yes-continuation",
        "steps": [
            "Ask: Which categories currently have no active shops?",
            "If the app asks whether to use categories with zero active shops, reply: yes",
        ],
        "expected": "Answer the original categories question; do not return generic chat.",
        "capture": [
            "request_id and conversation_id for both turns",
            "raw classifier intent and clarification fields for each turn",
            "pending clarification state before and after the yes reply",
            "final API response payload for each turn",
        ],
    },
    {
        "id": "pasted-sql-confirmation",
        "steps": [
            "Paste the discount SQL from the observed transcript.",
            "Confirm that the app should run it.",
        ],
        "expected": "Execute the pending pasted SQL and return its result.",
        "capture": [
            "request_id and conversation_id for paste and confirmation turns",
            "pending action/query before and after confirmation",
            "browser request/response payloads for both turns",
            "backend route logs associated with those request IDs",
        ],
    },
    {
        "id": "annual-total-summary",
        "steps": [
            "Ask: Which month had the most completed bookings in 2025?",
        ],
        "expected": (
            "If an annual total is stated, it must be 21; May and November "
            "are tied at 8 each. Do not state 16 as the annual total."
        ),
        "capture": [
            "request_id and conversation_id",
            "generated SQL and executed result rows",
            "result-grader verdict and summary-service input/output",
            "final API response payload and rendered frontend answer",
        ],
    },
]


def _validate_catalog(cases: List[Dict[str, Any]]) -> None:
    numbers = [case["number"] for case in cases]
    if numbers != list(range(66, 73)):
        raise ValueError("Catalog 4 must contain consecutive cases 66 through 72")
    for case in cases:
        if case["case_type"] not in {"sql", "conversation"}:
            raise ValueError(
                f"Case {case['number']} has unsupported case_type "
                f"{case['case_type']!r}"
            )
        if not case["golden_sql"]:
            raise ValueError(f"Case {case['number']} has no golden_sql")
        if case["case_type"] == "conversation" and not case["turns"]:
            raise ValueError(f"Case {case['number']} has no conversation turns")


_validate_catalog(GOLDEN_BENCHMARK_CASES)
