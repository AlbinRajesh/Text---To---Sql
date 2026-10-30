"""
test_business_rules.py — manual smoke test for business_rules.py (Node 5)

No database or LLM calls — this only tests the rulebook file + matching logic.

Run from backend/:
    python -m app.tests.test_business_rules
"""

import pytest

from app.agent.nodes.business_rules import find_relevant_rules, BusinessRulesError
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _apply_entity_resolution_constraints,
    _validate_active_offer_plan,
)
from app.agent.nodes.grade_sample import (
    _build_schema_grader_prompt,
    _is_irrelevant_active_offer_view_item,
)
from app.tests.golden_benchmark_catalog import GOLDEN_BENCHMARK_CASES


def test_top_shops_golden_case_applies_active_offer_rule() -> None:
    case = next(case for case in GOLDEN_BENCHMARK_CASES if case["number"] == 9)
    sql = case["golden_sql"].lower()

    assert "active offers" in case["question"].lower()
    assert "active_offer_status" in {
        rule["term"] for rule in find_relevant_rules(case["question"])
    }
    assert "o.status = 1" in sql
    assert "s.status" not in sql


def test_top_shops_active_offer_plan_requires_only_offer_status_filter() -> None:
    case = next(case for case in GOLDEN_BENCHMARK_CASES if case["number"] == 9)
    schema = {"offer": [{"column": "status", "data_type": "tinyint"}]}
    plan = {
        "table": "offer",
        "joins": [{"table": "shop_details"}],
        "where_expression": {
            "table": "offer",
            "column": "status",
            "operator": "=",
            "value": 1,
        },
    }

    _validate_active_offer_plan(plan, case["question"], schema)

    plan["where_expression"] = None
    with pytest.raises(QueryGenerationError, match="offer.status = 1"):
        _validate_active_offer_plan(plan, case["question"], schema)

    plan["where_expression"] = {
        "boolean": "AND",
        "conditions": [
            {
                "table": "offer",
                "column": "status",
                "operator": "=",
                "value": 1,
            },
            {
                "table": "shop_details",
                "column": "status",
                "operator": "=",
                "value": 1,
            },
        ],
    }
    with pytest.raises(QueryGenerationError, match="must not apply a shop_details.status"):
        _validate_active_offer_plan(plan, case["question"], schema)


def test_active_offer_join_conditions_and_active_shop_request_are_allowed() -> None:
    plan = {
        "table": "offer",
        "joins": [{
            "table": "shop_details",
            "conditions": [
                {
                    "table": "offer",
                    "column": "status",
                    "operator": "=",
                    "value": 1,
                },
                {
                    "table": "shop_details",
                    "column": "status",
                    "operator": "=",
                    "value": 1,
                },
            ],
        }],
        "where_expression": None,
    }
    schema = {"offer": [{"column": "status", "data_type": "tinyint"}]}

    _validate_active_offer_plan(
        plan,
        "List active offers from active shops.",
        schema,
    )

    with pytest.raises(QueryGenerationError, match="shop_details.status"):
        _validate_active_offer_plan(
            plan,
            "List active offers.",
            schema,
        )

    _validate_active_offer_plan(
        plan,
        "Show shops that are active with an active offer.",
        schema,
    )


def test_active_offer_location_case_uses_canonical_verified_path() -> None:
    case = next(case for case in GOLDEN_BENCHMARK_CASES if case["number"] == 13)
    sql = case["golden_sql"].lower()
    rules = find_relevant_rules(case["question"])
    prompt = _build_schema_grader_prompt(
        case["question"],
        case["question"],
        "offer: status, shop_id\nshop_details: id, name\nshop_address: shop_id, city, is_default",
        [
            {"table": "offer", "referenced_table": "shop_details"},
            {"table": "shop_details", "referenced_table": "shop_address"},
        ],
        matched_business_rules=rules,
    )

    assert "active_offer_status" in {rule["term"] for rule in rules}
    assert "offer -> shop_details -> shop_address" in prompt
    assert "Do not require a direct relationship from editable_offers_view to shop_address" in prompt
    assert "join offer o on s.id = o.shop_id" in sql
    assert "join shop_address a on a.shop_id = s.id" in sql
    assert "o.status = 1" in sql
    assert "a.is_default = 1" in sql
    assert "a.city = 'nagercoil'" in sql
    assert "s.status" not in sql


def test_active_offer_rule_ignores_view_relationship_requirements() -> None:
    item = {
        "type": "relationship",
        "detail": "join editable_offers_view to shop_address via shop_id",
        "tables": ["editable_offers_view", "shop_address"],
    }
    active_offer_rules = find_relevant_rules(
        "List active offers in Nagercoil with shop name."
    )

    assert _is_irrelevant_active_offer_view_item(item, active_offer_rules)
    assert not _is_irrelevant_active_offer_view_item(item, [])


def test_booking_discount_entity_resolution_uses_bookings_canonical_table():
    rule = next(
        rule for rule in find_relevant_rules(
            "On average, what percent discount do we give per booking?"
        )
        if rule["term"] == "overall_average_discount_percentage"
    )
    plan = {
        "answerable": True,
        "table": "bookings",
        "joins": [],
        "where_expression": None,
    }

    constrained = _apply_entity_resolution_constraints(
        plan,
        [rule],
        {"bookings": [
            {"column": "actual_amount", "data_type": "decimal"},
            {"column": "status", "data_type": "tinyint"},
        ]},
    )

    assert constrained["table"] == "bookings"
    assert constrained["joins"] == []


def test_service_popularity_rule_grounds_the_verified_booking_path():
    question = "Which service is booked the most?"
    rule = next(
        rule for rule in find_relevant_rules(question)
        if rule["term"] == "most_popular_service_aggregation"
    )
    schema = {
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "offer_id", "data_type": "bigint", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
        "offer": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "service_id", "data_type": "bigint", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
        "service": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "service_name", "data_type": "varchar", "key": ""},
        ],
    }
    relationships = [
        {
            "constraint_name": "reviewed_bookings_offer_id",
            "table": "bookings",
            "referenced_table": "offer",
            "cardinality": "many_to_one",
            "column_pairs": [{
                "column": "offer_id",
                "referenced_column": "id",
            }],
        },
        {
            "constraint_name": "reviewed_offer_service_id",
            "table": "offer",
            "referenced_table": "service",
            "cardinality": "many_to_one",
            "column_pairs": [{
                "column": "service_id",
                "referenced_column": "id",
            }],
        },
    ]

    constrained = _apply_entity_resolution_constraints(
        {
            "answerable": True,
            "table": "bookings",
            "joins": [],
            "where_expression": None,
        },
        [rule],
        schema,
        relationships,
    )

    assert [join["table"] for join in constrained["joins"]] == [
        "offer", "service",
    ]
    assert {
        (item["table"], item["column"], item["operator"], item.get("value"))
        for item in constrained["where_expression"]["conditions"]
    } == {
        ("bookings", "status", "=", 1),
        ("offer", "status", "=", 1),
    }


# (question, expected_term_or_None) — None means we expect zero matches
TEST_CASES = [
    (
        "How many shops have an average rating above 4.5?",
        "shops_above_average_rating_threshold",
    ),
    (
        "Show shops with average rating above 4.5",
        "list_shops_above_average_rating_threshold",
    ),
    ("What's the weather like today", None),
]


def main() -> None:
    try:
        for question, expected_term in TEST_CASES:
            print(f"Question: {question!r}")
            matched = find_relevant_rules(question)
            matched_terms = [rule["term"] for rule in matched]
            print(f"   Matched terms: {matched_terms}")

            if expected_term is None:
                status = "OK" if not matched_terms else "UNEXPECTED MATCH"
            else:
                status = "OK" if expected_term in matched_terms else "MISSING EXPECTED MATCH"
            print(f"   Result: {status}\n")

        print("All checks completed.")

    except BusinessRulesError as e:
        print(f"FAILED to load rulebook: {e}")


if __name__ == "__main__":
    main()