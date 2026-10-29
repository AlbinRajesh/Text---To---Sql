from app.agent.nodes.business_rules import BusinessRulesStore


def test_existing_business_rules_match_their_target_questions():
    store = BusinessRulesStore()
    cases = {
        "How many users have a subscription plan right now?": "subscription_plan",
        "What's the most expensive subscription plan we offer?":
            "active_plan_catalog",
        "Are there any good restaurants in Colachel?":
            "restaurant_quirk_and_rating_filter",
        "Show me all of the vegan restaurants.":
            "vegan_subcategory_lookup",
        "are there any hotels that aren't listed as restaurants?":
            "hotel_name_non_restaurant_filter",
    }

    for question, expected_term in cases.items():
        matched_terms = {
            rule["term"] for rule in store.find_relevant_rules(question)
        }
        assert expected_term in matched_terms
        if expected_term == "active_plan_catalog":
            assert not matched_terms.intersection({
                "subscription_plan",
                "ontology_subscription_plan",
                "plan_subscription_existence",
                "ontology_premium_plan",
            })


def test_plan_catalog_question_does_not_match_user_subscription_rule():
    store = BusinessRulesStore()
    matched_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "Which plan is the most popular among our users?"
        )
    }

    assert "subscription_plan" not in matched_terms
    assert "ontology_subscription_plan" not in matched_terms


def test_premium_plan_catalog_question_does_not_match_user_subscription_rules():
    store = BusinessRulesStore()
    matched_terms = {
        rule["term"]
        for rule in store.find_relevant_rules("List all premium subscription plans.")
    }

    assert "plan_subscription_existence" not in matched_terms
    assert "ontology_premium_plan" not in matched_terms


def test_user_question_about_buying_a_premium_plan_matches_subscription_rule():
    store = BusinessRulesStore()
    matched_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "Who bought a premium plan?"
        )
    }

    assert "plan_subscription_existence" in matched_terms


def test_city_consumable_search_requires_search_intent_and_location_phrase():
    store = BusinessRulesStore()
    city_search_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "I'm thirsty, where can I get a juice in Nagercoil?"
        )
    }
    named_shop_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "Give me the phone number for A1 Juice house."
        )
    }
    named_shop_review_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "what are people saying about A1 Juice house?"
        )
    }

    assert "keyword_shop_search_by_city" in city_search_terms
    city_search_rule = next(
        rule for rule in store.find_relevant_rules(
            "I'm thirsty, where can I get a juice in Nagercoil?"
        )
        if rule["term"] == "keyword_shop_search_by_city"
    )
    assert city_search_rule["search_scope"] == "shop_names"
    assert city_search_rule["entity_resolution"]["related_entities"] == [
        {"table": "shop_address", "required": True}
    ]
    assert "keyword_shop_search_by_city" not in named_shop_terms
    assert "keyword_shop_search_by_city" not in named_shop_review_terms


def test_active_plan_catalog_rule_requires_active_plan_status():
    store = BusinessRulesStore()
    rule = next(
        rule for rule in store.find_relevant_rules(
            "What's the most expensive subscription plan we offer?"
        )
        if rule["term"] == "active_plan_catalog"
    )

    assert rule["entity_resolution"] == {
        "canonical_table": "plans",
        "filters": [
            {
                "table": "plans",
                "column": "status",
                "operator": "=",
                "value": 1,
            },
        ],
    }


def test_case_specific_search_rules_use_narrow_search_scopes():
    store = BusinessRulesStore()
    vegan_rules = {
        rule["term"] for rule in store.find_relevant_rules(
            "Show me all the vegan restaurants."
        )
    }
    vegan_rule = next(
        rule for rule in store.find_relevant_rules(
            "Show me all the vegan restaurants."
        )
        if rule["term"] == "vegan_subcategory_lookup"
    )
    hotel_rule = next(
        rule for rule in store.find_relevant_rules(
            "are there any hotels that aren't listed as restaurants?"
        )
        if rule["term"] == "hotel_name_non_restaurant_filter"
    )

    assert "subcategory_restaurant" not in vegan_rules
    assert vegan_rule["catalog_resolution"] == "rule_defined"
    assert vegan_rule["search_operation"] == {
        "term": "vegan",
        "scope": "shop_subcategories",
    }
    assert hotel_rule["catalog_resolution"] == "rule_defined"
    assert hotel_rule["search_operation"] == {
        "term": "hotel",
        "scope": "shop_names",
    }
    assert hotel_rule["query_logic"]["where_expression"]["conditions"][0] == {
        "table": "subcategories",
        "column": "subcategoryname",
        "operator": "IS NULL",
    }


def test_count_shops_at_least_average_rating_threshold_matches_only_at_least():
    store = BusinessRulesStore()
    at_least_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "How many shops have an average rating of at least 4.0?"
        )
    }
    below_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "How many shops have an average rating below 4.0?"
        )
    }

    assert "shops_at_least_average_rating_threshold" in at_least_terms
    assert "shops_at_least_average_rating_threshold" not in below_terms


def test_list_shops_at_least_average_rating_threshold_matches_only_at_least():
    store = BusinessRulesStore()
    at_least_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "List shops with an average rating of at least 4.0."
        )
    }
    below_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "List shops with an average rating below 4.0."
        )
    }

    assert "list_shops_at_least_average_rating_threshold" in at_least_terms
    assert "list_shops_at_least_average_rating_threshold" not in below_terms


def test_count_shops_below_average_rating_threshold_matches_only_below():
    store = BusinessRulesStore()
    below_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "How many shops have an average rating below 3.0?"
        )
    }
    at_least_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "How many shops have an average rating of at least 3.0?"
        )
    }

    assert "shops_below_average_rating_threshold" in below_terms
    assert "shops_below_average_rating_threshold" not in at_least_terms


def test_list_shops_below_average_rating_threshold_matches_only_below():
    store = BusinessRulesStore()
    below_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "List shops with an average rating below 3.0."
        )
    }
    at_least_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "List shops with an average rating of at least 3.0."
        )
    }

    assert "list_shops_below_average_rating_threshold" in below_terms
    assert "list_shops_below_average_rating_threshold" not in at_least_terms
