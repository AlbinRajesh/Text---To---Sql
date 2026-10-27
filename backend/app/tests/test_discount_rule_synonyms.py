from app.agent.nodes.business_rules import find_relevant_rules


def _terms(question):
    return [rule["term"] for rule in find_relevant_rules(question)]


def test_discounted_offer_price_is_not_a_discount_value_question():
    assert "discount value" not in _terms(
        "What is the average discounted offer price for shops whose name contains 'Juice'?"
    )


def test_total_discount_still_matches_the_discount_value_rule():
    assert "discount_value" in _terms("What is the total discount offered?")


def test_booking_discount_rule_does_not_match_offer_discount_percentage_rule():
    question = "On average, what percent discount do we give per booking?"
    terms = _terms(question)

    assert "overall_average_discount_percentage" in terms
    assert "discount_percent" not in terms


def test_offer_discount_percentage_rule_still_matches_offer_questions():
    assert "discount_percent" in _terms(
        "Which offer has the highest discount percentage?"
    )


def test_maximum_savings_offer_rule_is_registered():
    for question in (
        "Which deal saves me the most money?",
        "Find the best deal.",
    ):
        assert "max_discount_amount_offer" in _terms(question)