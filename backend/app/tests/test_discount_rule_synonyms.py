from app.agent.nodes.business_rules import find_relevant_rules


def _terms(question):
    return [rule["term"] for rule in find_relevant_rules(question)]


def test_discounted_offer_price_is_not_a_discount_value_question():
    assert "discount value" not in _terms(
        "What is the average discounted offer price for shops whose name contains 'Juice'?"
    )


def test_total_discount_still_matches_the_discount_value_rule():
    assert "discount value" in _terms("What is the total discount offered?")