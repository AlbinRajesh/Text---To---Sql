from app.agent.nodes.business_rules import BusinessRulesStore


def test_new_business_rules_match_target_questions():
    store = BusinessRulesStore()
    cases = {
        "How many users are currently active on our platform?": "active_user",
        "How many users have a subscription plan right now?": "subscription_plan",
        "Which plan is the most popular among our users?": "subscription_plan",
        "Are there any good restaurants in Colachel?": "good_restaurants",
    }

    for question, expected_term in cases.items():
        matched_terms = {
            rule["term"] for rule in store.find_relevant_rules(question)
        }
        assert expected_term in matched_terms
