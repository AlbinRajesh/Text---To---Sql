"""Query-plan benchmark for the advanced business questions.

Run from ``backend`` with:
    python -m app.tests.advanced_query_generation_benchmark
    python -m app.tests.advanced_query_generation_benchmark --cases 6,10,14-16

This classifies and rewrites each question, generates a structured query plan,
validates the plan, and runs ``EXPLAIN`` against the configured MySQL database.
It never executes generated SQL against business rows and does not grade or
record final answers.
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from app.agent.nodes.generate_query import QueryGenerationError
from app.services import llm_service
from app.agent.nodes.schema_grounding import (
    get_cached_schema,
    refresh_schema_cache,
)
from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
)
from app.services.embedding_service import build_schema_index
from app.tests.query_generation_benchmark import (
    _case,
    _parse_case_numbers as _parse_case_numbers_impl,
    _run_case,
)

logger = logging.getLogger(__name__)


def _advanced_case(
    number: int,
    question: str,
    tables: Sequence[str],
    *,
    joins: Sequence[Tuple[str, str] | Tuple[str, str, str]] = (),
    selects: Sequence[Tuple[str, str, str | None, bool]] = (),
    filters: Sequence[Tuple[str, str, str, Any]] = (),
    groups: Sequence[Tuple[str, str]] = (),
    having: Sequence[Tuple[str, str, str, str, Any]] = (),
    order_by: Sequence[Tuple[str, str]] = (),
    limit: int | None = None,
    round_digits: int | None = None,
    filter_boolean: str | None = None,
    expected_outcome: str = "sql",
    expected_rule_id: str | None = None,
    expected_stage: str | None = None,
    note: str = "",
    alternatives: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    category = "advanced"
    case = _case(
        number,
        category,
        question,
        tables,
        joins=joins,
        selects=selects,
        filters=filters,
        groups=groups,
        having=having,
        order_by=order_by,
        limit=limit,
        round_digits=round_digits,
        filter_boolean=filter_boolean,
        expected_outcome=expected_outcome,
        note=note,
        alternatives=alternatives,
    )
    case["category"] = category
    case["expected_rule_id"] = expected_rule_id
    case["expected_stage"] = expected_stage
    return case


CASES = [
    _advanced_case(
        1,
        "What is the full address of the highest-rated shop in Kazhakkoottam?",
        ["shop_details", "shop_address", "reviews"],
        expected_outcome="safe_block",
        note="A limited row query must not treat the highest individual review rating as the shop's rating.",
    ),
    _advanced_case(
        2,
        "List the names and landmarks of shops in Tamil Nadu offering a discount of more than 500 rupees.",
        ["shop_details", "shop_address", "offer"],
        expected_outcome="refusal",
        note="Official discount is offer.actual_price - offer.offer_price; the plan format has no arithmetic, and bookings.discount is a different thing. Must refuse.",
    ),
    _advanced_case(
        3,
        "Which shop in Kerala has the highest total booking revenue and an average rating above 4.0?",
        ["bookings", "shop_details", "shop_address", "reviews"],
        expected_outcome="safe_block",
        note="Booking revenue and review ratings are two one-to-many aggregates on one shop; the fan-out guard must reject it.",
    ),
    _advanced_case(
        4,
        "Give me the phone number and area road name of the shop that has the 'Lucky Users Special Offer!'.",
        ["shop_details", "shop_address", "offer"],
        joins=[
            ("shop_details", "reviewed_offer_shop_id"),
            ("shop_address", "reviewed_shop_address_shop_id"),
        ],
        selects=[
            ("shop_details", "phone", None, False),
            ("shop_address", "arearoadname", None, False),
        ],
        filters=[
            ("offer", "offer_heading", "=", "Lucky Users Special Offer!"),
            ("shop_address", "is_default", "=", 1),
        ],
        note="Join offer to its shop and the shop's default address; filter by offer heading.",
        alternatives=[
            _advanced_case(
                4,
                "Give me the phone number and area road name of the shop that has the 'Lucky Users Special Offer!'.",
                ["shop_details", "shop_address", "offer"],
                joins=[
                    ("offer", "reviewed_offer_shop_id"),
                    ("shop_address", "reviewed_shop_address_shop_id"),
                ],
                selects=[
                    ("shop_details", "phone", None, False),
                    ("shop_address", "arearoadname", None, False),
                ],
                filters=[
                    ("offer", "offer_heading", "=", "Lucky Users Special Offer!"),
                    ("shop_address", "is_default", "=", 1),
                ],
                note="Equivalent plan rooted at shop_details, joining the offer and default address.",
            ),
        ],
    ),
    _advanced_case(
        5,
        "How many shops located in Perumselvavilai have zero bookings?",
        ["shop_details", "shop_address", "bookings"],
        joins=[
            ("shop_address", "reviewed_shop_address_shop_id"),
            ("bookings", "reviewed_bookings_shop_id", "LEFT"),
        ],
        selects=[("shop_details", "id", "COUNT", True)],
        filters=[
            ("shop_address", "city", "=", "Perumselvavilai"),
            ("shop_address", "is_default", "=", 1),
            ("bookings", "id", "IS NULL", None),
        ],
        filter_boolean="AND",
        note="Default-address shops in the city with no booking row; LEFT JOIN anti-match, count distinct shop id.",
    ),
    _advanced_case(
        6,
        "Find the average booking value for shops in the 'Restaurent' subcategory.",
        ["bookings", "shop_details", "subcategories"],
        joins=[
            ("shop_details", "reviewed_bookings_shop_id"),
            ("subcategories", "reviewed_shop_details_subcategory_id"),
        ],
        selects=[
            ("bookings", "final_amount", "AVG", False),
        ],
        filters=[("subcategories", "subcategoryname", "=", "Restaurent")],
        note="Average bookings.final_amount through the verified shop and subcategory relationships.",
    ),
    _advanced_case(
        7,
        "List the top 3 categories by total discount value offered right now.",
        ["categories", "shop_details", "offer"],
        expected_outcome="refusal",
        note="Refuse because 'right now' has no configured date policy and discount value requires unsupported price subtraction.",
    ),
    _advanced_case(
        8,
        "Which city has the lowest average rating for shops in the 'Transportation Services' category?",
        ["shop_address", "shop_details", "categories", "reviews"],
        expected_outcome="safe_block",
        note="Averages reviews.rating from a one-to-many child while joined to the address table; refused by the fan-out guard until Phase 5 decides whether to allow verified one-address-per-shop joins.",
    ),
    _advanced_case(
        9,
        "Find the highest-rated shop that offers 'à®•à¯à®±à¯ˆà®¨à¯à®¤ à®µà®¿à®²à¯ˆà®¯à®¿à®²à¯ à®®à®°à®™à¯à®•à®³à¯ à®µà¯†à®Ÿà¯à®Ÿà®¿ à®¤à®° à®ªà®Ÿà¯à®®à¯'.",
        ["shop_details", "offer", "reviews"],
        expected_outcome="safe_block",
        note="Aggregating reviews while joining offers can multiply review rows; the fan-out guard should reject it.",
    ),
    _advanced_case(
        10,
        "List the names and phone numbers of shops in the 'Food & Dining' category that have at least 3 offers.",
        ["offer", "shop_details", "categories"],
        joins=[
            ("categories", "reviewed_shop_details_category_id"),
            ("offer", "reviewed_offer_shop_id", "LEFT"),
        ],
        selects=[
            ("shop_details", "name", None, False),
            ("shop_details", "phone", None, False),
        ],
        filters=[("categories", "categoriesname", "=", "Food & Dining")],
        groups=[
            ("shop_details", "id"),
            ("shop_details", "name"),
            ("shop_details", "phone"),
        ],
        having=[("offer", "id", "COUNT", ">=", 3)],
        note="Shops in the named category with at least 3 offers: HAVING COUNT(offer.id) >= 3, grouped by shop; names and phones only.",
    ),
    _advanced_case(
        11,
        "Find the top 3 categories that generated the highest total booking revenue.",
        ["bookings", "shop_details", "categories"],
        joins=[
            ("shop_details", "reviewed_bookings_shop_id"),
            ("categories", "reviewed_shop_details_category_id"),
        ],
        selects=[
            ("categories", "categoriesname", None, False),
            ("bookings", "final_amount", "SUM", False),
        ],
        groups=[("categories", "id"), ("categories", "categoriesname")],
        order_by=[("revenue", "DESC")],
        limit=3,
        note="Aggregate bookings.final_amount before grouping by the verified shop category.",
    ),
    _advanced_case(
        12,
        "Give me a breakdown of cancelled subscriptions: how many are there in total, and what is the total amount lost from them?",
        ["subscriptions"],
        selects=[
            ("subscriptions", "id", "COUNT", False),
            ("subscriptions", "amount", "SUM", False),
        ],
        filters=[("subscriptions", "status", "=", "cancelled")],
        note="Checks the COUNT and SUM query plan; it does not inspect SQL NULL or present an answer.",
    ),
    _advanced_case(
        13,
        "How many users do not have any plan assigned to them and have never made a booking?",
        ["users", "bookings"],
        joins=[("bookings", "reviewed_bookings_requester_id", "LEFT")],
        selects=[("users", "id", "COUNT", True)],
        filters=[
            ("users", "plan_id", "IS NULL", None),
            ("bookings", "id", "IS NULL", None),
        ],
        filter_boolean="AND",
        note="LEFT JOIN anti-match: users with no plan and no booking row; count distinct users.id.",
    ),
    _advanced_case(
        14,
        "Are there any top-level categories that have absolutely zero offers posted across all their shops?",
        ["categories", "shop_details", "offer"],
        joins=[
            ("shop_details", "reviewed_shop_details_category_id", "LEFT"),
            ("offer", "reviewed_offer_shop_id", "LEFT"),
        ],
        selects=[("categories", "categoriesname", None, False)],
        groups=[("categories", "id"), ("categories", "categoriesname")],
        having=[("offer", "id", "COUNT", "=", 0)],
        note="Use a LEFT JOIN chain and group by both the category key and label.",
    ),
    _advanced_case(
        15,
        "Which subcategory has the highest average review rating, and how many unique users have left a review for shops in that subcategory?",
        ["reviews", "shop_details", "subcategories", "users"],
        joins=[
            ("shop_details", "reviewed_reviews_shop_id"),
            ("subcategories", "reviewed_shop_details_subcategory_id"),
            ("users", "reviewed_reviews_user_id"),
        ],
        selects=[
            ("subcategories", "subcategoryname", None, False),
            ("reviews", "rating", "AVG", False),
            ("users", "id", "COUNT", True),
        ],
        groups=[
            ("subcategories", "id"),
            ("subcategories", "subcategoryname"),
        ],
        order_by=[("average_rating", "DESC")],
        limit=1,
        note="Reviews as base for the average; join users and count distinct users.id for unique reviewers.",
    ),
    _advanced_case(
        16,
        "List the names of shops that have an average review rating of at least 4.0 and also have a total booking revenue greater than 5000.",
        ["shop_details", "reviews", "bookings"],
        expected_outcome="safe_block",
        note="Combining review and booking aggregates across two one-to-many relations risks fan-out; require a safe refusal.",
    ),
    _advanced_case(
        17,
        "Show me the products available at the highest-rated restaurant in Nagercoil.",
        ["products", "shop_details", "reviews", "bookings"],
        expected_outcome="refusal",
        note="No verified relationship connects products to shops.",
    ),
    _advanced_case(
        18,
        "List the names of users who have booked a shop in the 'Fashions' category and also hold an active subscription.",
        ["users", "bookings", "shop_details", "categories", "subscriptions"],
        expected_outcome="refusal",
        note="The users-to-subscriptions relationship is not verified; do not infer customer_id mapping.",
    ),
    _advanced_case(
        19,
        "What is the total value of currently active offers?",
        ["offer"],
        expected_outcome="refusal",
        note="Relative dates cannot be interpreted until a timezone and calendar policy is configured.",
    ),
    _advanced_case(
        20,
        "Show the shop name and pincode for all shops that have a vehicle number listed in their details.",
        ["shop_details", "shop_address"],
        joins=[("shop_address", "reviewed_shop_address_shop_id")],
        selects=[
            ("shop_details", "name", None, False),
            ("shop_address", "pincode", None, False),
        ],
        filters=[
            ("shop_details", "vechicle_no", "IS NOT NULL", None),
            ("shop_address", "is_default", "=", 1),
        ],
        filter_boolean="AND",
        order_by=[("name", "ASC")],
        note="Join each shop to its default address; shops with a non-null vechicle_no (column name is spelled this way in the database).",
    ),
    _advanced_case(
        21,
        "Which category has the highest number of shops physically located in 'Thiruvananthapuram'?",
        ["categories", "shop_details", "shop_address"],
        joins=[
            ("shop_details", "reviewed_shop_details_category_id", "LEFT"),
            ("shop_address", "reviewed_shop_address_shop_id", "LEFT"),
        ],
        selects=[
            ("categories", "categoriesname", None, False),
            ("shop_details", "id", "COUNT", True),
        ],
        filters=[
            ("shop_address", "is_default", "=", 1),
            ("shop_address", "city", "=", "Thiruvananthapuram"),
        ],
        groups=[("categories", "id"), ("categories", "categoriesname")],
        order_by=[("shop_count", "DESC")],
        limit=1,
        note="Count distinct shops per category, restricted to default addresses in the city.",
    ),
    _advanced_case(
        22,
        "List the names of shops in the 'Services' category that have a verified default address and are currently active.",
        ["shop_details", "categories", "shop_address"],
        expected_outcome="refusal",
        note="No 'verified address' column exists and 'currently active' has no definition. Grader escalates today; accepted as a known gap.",
    ),
    _advanced_case(
        23,
        "What is the average discounted offer price for shops that have the word 'Juice' in their name?",
        ["offer", "shop_details"],
        joins=[("shop_details", "reviewed_offer_shop_id")],
        selects=[("offer", "offer_price", "AVG", False)],
        filters=[("shop_details", "name", "LIKE", "%Juice%")],
        note="Average offer_price for shops matched by name using the verified offer/shop relationship.",
    ),
    _advanced_case(
        24,
        "Find the average review rating for products listed by shops in Kerala.",
        ["products", "shop_details", "reviews", "shop_address"],
        expected_outcome="refusal",
        note="No verified relationship connects products to shops; the requested Kerala location also needs the unverified address relation.",
    ),
    _advanced_case(
        25,
        "List the shop name, category name, and average review rating for all shops in the 'Fashions' category, ordered from highest to lowest rating.",
        ["reviews", "shop_details", "categories"],
        joins=[
            ("shop_details", "reviewed_reviews_shop_id"),
            ("categories", "reviewed_shop_details_category_id"),
        ],
        selects=[
            ("shop_details", "name", None, False),
            ("categories", "categoriesname", None, False),
            ("reviews", "rating", "AVG", False),
        ],
        filters=[("categories", "categoriesname", "=", "Fashions")],
        groups=[
            ("shop_details", "id"),
            ("shop_details", "name"),
            ("categories", "id"),
            ("categories", "categoriesname"),
        ],
        order_by=[("avg_rating", "DESC")],
        note="Rank shops by average review rating, grouping by shop and category identifiers and names.",
    ),
]

# Restore the authoritative UTF-8 Tamil text for case 9 from the benchmark source data.
CASES = [
    {
        **case,
        "question": (
            "Find the highest-rated shop that offers "
            "'குறைந்த விலையில் மரங்கள் வெட்டி தர படும்'."
        ),
    }
    if case["number"] == 9
    else case
    for case in CASES
]


CSV_FIELDS = [
    "case_number",
    "category",
    "question",
    "rewritten_question",
    "expected_outcome",
    "generation_status",
    "retrieved_tables_by_attempt",
    "selected_schema_tables",
    "expected_tables",
    "verified_relationships_available",
    "matched_business_rules",
    "generated_base_table",
    "generated_joins",
    "generated_select_items",
    "generated_filters",
    "generated_group_by",
    "generated_having",
    "generated_order_by",
    "generated_query_plan",
    "compiled_sql",
    "sql_parameters",
    "safety_validation",
    "explain_validation",
    "plan_match",
    "plan_check_failures",
    "expected_behavior",
    "error",
    "outcome",
    "result",
    "rule_id",
    "error_stage",
    "llm_calls_total",
    "llm_calls_by_role",
    "llm_attempts_by_provider",
]


def _install_llm_recorder(sink: List[Dict[str, Any]]):
    """Wraps llm_service.record_llm_call to count logical calls and attempts."""
    original = llm_service.record_llm_call

    def wrapper(**kwargs):
        sink.append({key: kwargs.get(key) for key in ("role", "provider", "attempts", "ok")})
        return original(**kwargs)

    llm_service.record_llm_call = wrapper
    return original


def _classify_outcome(row: Dict[str, Any]) -> str:
    """Classify evaluated outcomes separately from transient service failures."""
    if not row.get("error"):
        return "answered"
    bases = {name for name in str(row.get("error_bases", "")).split(",") if name}
    if bases & {"UnanswerableQuestionError", "MissingRelationshipError"}:
        return "confirmed_refusal"
    if "SchemaGraderUnavailableError" in bases:
        return "not_evaluated"
    if "GradeSampleError" in bases:
        return "escalated"
    if bases & {"LLMServiceError", "LLMConfigError"} or row.get("error_cause") in {
        "LLMServiceError",
        "LLMConfigError",
    }:
        return "not_evaluated"
    if "MySQL EXPLAIN rejected generated SQL" in str(row.get("error", "")):
        return "sql_rejected_by_database"
    if bases & {"QueryGenerationError", "SafetyViolationError"}:
        return "validation_rejected"
    return "unclassified_error"


def _score(case: Dict[str, Any], row: Dict[str, Any]) -> Tuple[str, str]:
    """Returns ('pass' | 'fail' | 'not_evaluated', reason)."""
    outcome = row["outcome"]
    expected = case["expected_outcome"]
    if outcome == "not_evaluated":
        return "not_evaluated", "provider failure; this case did not complete evaluation"
    if expected == "sql":
        if outcome == "answered" and row.get("plan_match") == "yes":
            return "pass", ""
        return "fail", f"expected a matching SQL plan; outcome={outcome}"
    if expected == "safe_block":
        if outcome == "confirmed_refusal":
            return "pass", ""
        if (
            outcome == "validation_rejected"
            and not row.get("compiled_sql")
            and row.get("safety_validation") != "passed"
            and row.get("explain_validation") != "passed"
        ):
            return "pass", ""
        return "fail", (
            "expected a confirmed refusal or pre-SQL validation rejection; "
            f"outcome={outcome}"
        )
    if expected == "refusal":
        if outcome == "confirmed_refusal":
            return "pass", ""
        return "fail", f"expected a confirmed refusal; outcome={outcome}"
    if expected == "safety_refusal":
        want_rule = case.get("expected_rule_id")
        want_stage = case.get("expected_stage")
        if (
            outcome == "validation_rejected"
            and row.get("rule_id") == want_rule
            and row.get("error_stage") == want_stage
        ):
            return "pass", ""
        return "fail", (
            f"expected rule_id={want_rule} at stage={want_stage}; "
            f"got outcome={outcome} rule_id={row.get('rule_id')!r} "
            f"stage={row.get('error_stage')!r}"
        )
    return "fail", f"unknown expected_outcome {expected!r}"


async def _run(
    output_path: Path,
    selected: set[int] | None = None,
    pause_seconds: float = 0.0,
) -> None:
    if output_path.exists():
        raise SystemExit(
            f"{output_path} already exists. Choose a new --output so old results are never overwritten."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await connect_to_mysql()
    try:
        await connect_to_read_replica()
        await refresh_schema_cache()
        await build_schema_index(get_cached_schema())

        cases_to_run = [
            case for case in CASES
            if selected is None or case["number"] in selected
        ]
        if not cases_to_run:
            raise SystemExit(f"No advanced benchmark cases match {sorted(selected or [])}.")

        rows = []
        for index, case in enumerate(cases_to_run):
            logger.info("Benchmarking advanced case %d/%d", case["number"], len(CASES))
            calls: List[Dict[str, Any]] = []
            original_recorder = _install_llm_recorder(calls)
            try:
                row = await _run_case(case)
            finally:
                llm_service.record_llm_call = original_recorder

            row["outcome"] = _classify_outcome(row)
            result, reason = _score(case, row)
            row["result"] = result
            if case["expected_outcome"] == "safety_refusal":
                row["plan_match"] = "yes" if result == "pass" else "no"
                row["plan_check_failures"] = json.dumps([reason] if reason else [])
            by_role = Counter(call["role"] for call in calls)
            attempts: Counter = Counter()
            for call in calls:
                attempts[call["provider"]] += int(call.get("attempts") or 0)
            row["llm_calls_total"] = len(calls)
            row["llm_calls_by_role"] = json.dumps(dict(by_role), sort_keys=True)
            row["llm_attempts_by_provider"] = json.dumps(dict(attempts), sort_keys=True)
            row["expected_outcome"] = case["expected_outcome"]
            row["expected_behavior"] = case["reference_note"]
            rows.append({field: row.get(field, "") for field in CSV_FIELDS})
            if pause_seconds and index + 1 < len(cases_to_run):
                await asyncio.sleep(pause_seconds)

        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        total = len(rows)
        outcomes = Counter(row["outcome"] for row in rows)
        print(f"Wrote {total} query-plan diagnostics to {output_path}")
        evaluated = [row for row in rows if row["result"] != "not_evaluated"]
        passed = sum(row["result"] == "pass" for row in evaluated)
        not_evaluated = total - len(evaluated)
        print(
            f"Met expectation: {passed}/{len(evaluated)} evaluated; "
            f"not evaluated: {not_evaluated}/{total}"
        )
        print("Outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
        print(
            f"Plans generated: {sum(r['generation_status'] == 'generated' for r in rows)}; "
            f"safety check passed: {sum(r['safety_validation'] == 'passed' for r in rows)}; "
            f"EXPLAIN passed: {sum(r['explain_validation'] == 'passed' for r in rows)}."
        )
        print("Generated SQL was never executed. No returned rows or answers were evaluated.")
    finally:
        await close_mysql_connection()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name(
            f"advanced_benchmark_{datetime.now():%Y%m%d_%H%M%S}.csv"
        ),
        help="CSV destination (default: a new timestamped file; existing files are never overwritten)",
    )
    parser.add_argument(
        "--cases",
        type=str,
        default=None,
        help="Run selected case numbers, e.g. '6,10,14-16' (default: all 25)",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.0,
        help="Optional pause between cases to reduce provider rate-limit pressure.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List question numbers and expected outcomes without connecting to services.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.list:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        for case in CASES:
            print(f"{case['number']:02d} [{case['expected_outcome']}] {case['question']}")
        return
    if args.pause_seconds < 0:
        parser.error("--pause-seconds must be non-negative.")
    selected = _parse_case_numbers_impl(args.cases) if args.cases else None
    asyncio.run(_run(args.output.resolve(), selected, args.pause_seconds))


if __name__ == "__main__":
    main()
