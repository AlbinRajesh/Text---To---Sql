"""
test_business_rules.py — manual smoke test for business_rules.py (Node 5)

No database or LLM calls — this only tests the rulebook file + matching logic.

Run from backend/:
    python -m app.tests.test_business_rules
"""

from app.agent.nodes.business_rules import find_relevant_rules, BusinessRulesError

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