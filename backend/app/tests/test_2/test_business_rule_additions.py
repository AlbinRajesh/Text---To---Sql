import asyncio

from app.agent import graph
from app.agent.nodes.business_rules import BusinessRulesStore
from app.agent.nodes.generate_query import _apply_entity_resolution_constraints


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


def test_reported_questions_match_their_canonical_business_rules():
    store = BusinessRulesStore()
    cases = {
        "Are there any good restaurants in Colachel?":
            "restaurant_quirk_and_rating_filter",
        "Show me all the partners who are on a paid plan but have 0 active shops.":
            "paid_partner_zero_shops_audit",
        "How much potential recurring revenue do we have from active plans?":
            "mrr_calculation",
        "Who are our oldest subscribers based on their start date?":
            "oldest_active_subscribers",
        "Which plans allow for more than 5 products?":
            "plan_product_limit_threshold",
        "How many active users do not have a plan assigned?":
            "active_users_without_plan",
        "How many users are on each active plan?":
            "users_per_active_plan",
        "List the names of all the plans we currently have.":
            "active_plans_listing",
        "How many active offers have a redemption limit set?":
            "active_offer_status",
        "Which active offers end this month?":
            "active_offer_status",
        "Which shops in Trivandrum have the worst ratings?":
            "worst_rated_shops_city_filter",
        "On average, what percent discount do we give per booking?":
            "overall_average_discount_percentage",
        "Which shops haven't put up any offers yet?":
            "shops_without_offers",
    }

    for question, expected_term in cases.items():
        matched_terms = {
            rule["term"] for rule in store.find_relevant_rules(question)
        }
        assert expected_term in matched_terms, question

    mrr_terms = {
        rule["term"]
        for rule in store.find_relevant_rules(
            "How much potential recurring revenue do we have from active plans?"
        )
    }
    assert "active_plan_catalog" not in mrr_terms


def test_original_and_rewritten_questions_both_contribute_business_rules():
    state = {
        "user_question": "What good restaurants can I find?",
        "classifier_result": {
            "original_question": "What good restaurants can I find?",
            "rewritten_question": "What restaurants can I find?",
        },
    }

    asyncio.run(graph.node_business_rules(state))

    assert "restaurant_quirk_and_rating_filter" in {
        rule["term"] for rule in state["matched_business_rules"]
    }


def test_business_rule_where_expressions_are_enforced_on_validated_plans():
    store = BusinessRulesStore()
    cases = [
        (
            "How many active users do not have a plan assigned?",
            "users",
            {"status": "tinyint", "plan_id": "int"},
            "active_users_without_plan",
            {("users", "plan_id", "IS NULL", None), ("users", "plan_id", "=", "0")},
        ),
        (
            "Which plans allow for more than 5 products?",
            "plans",
            {"status": "tinyint", "product_limit": "int"},
            "plan_product_limit_threshold",
            {("plans", "product_limit", ">", "5")},
        ),
        (
            "How many active offers have a redemption limit set?",
            "offer",
            {"status": "tinyint", "limit": "varchar"},
            "offer_redemption_limit_set",
            {("offer", "limit", "IS NOT NULL", None), ("offer", "limit", "!=", "0")},
        ),
        (
            "On average, what percent discount do we give per booking?",
            "bookings",
            {"status": "tinyint", "actual_amount": "decimal"},
            "overall_average_discount_percentage",
            {
                ("bookings", "status", "=", "1"),
                ("bookings", "actual_amount", ">", "0"),
            },
        ),
    ]

    for question, table, column_types, rule_term, expected in cases:
        schema = {
            table: [
                {"column": column, "data_type": data_type}
                for column, data_type in column_types.items()
            ]
        }
        rule = next(
            rule for rule in store.find_relevant_rules(question)
            if rule["term"] == rule_term
        )
        plan = _apply_entity_resolution_constraints(
            {"answerable": True, "table": table, "joins": [], "where_expression": None},
            [rule],
            schema,
        )
        leaves = _condition_leaves(plan["where_expression"])
        observed = {
            (
                condition.get("table"),
                condition["column"],
                condition["operator"],
                None if "value" not in condition else str(condition["value"]),
            )
            for condition in leaves
        }
        assert expected.issubset(observed), question


def test_oldest_subscribers_contract_enforces_order_and_five_row_limit():
    store = BusinessRulesStore()
    rule = next(
        rule for rule in store.find_relevant_rules(
            "Who are our oldest subscribers based on their start date?"
        )
        if rule["term"] == "oldest_active_subscribers"
    )
    schema = {
        "users": [
            {"column": "subscriber_status", "data_type": "tinyint"},
            {"column": "plan_id", "data_type": "int"},
            {"column": "subscriber_startDate", "data_type": "datetime"},
        ]
    }

    plan = _apply_entity_resolution_constraints(
        {
            "answerable": True,
            "table": "users",
            "joins": [],
            "where_expression": None,
            "order_by": [],
            "limit": None,
            "ranking_intent": "none",
        },
        [rule],
        schema,
    )

    assert plan["limit"] == 5
    assert plan["order_by"] == [{
        "table": "users",
        "column": "subscriber_startDate",
        "direction": "ASC",
    }]
    assert plan["ranking_intent"] == "top_n"


def _condition_leaves(expression):
    if expression is None:
        return []
    if expression.get("boolean") in {"AND", "OR"}:
        return [
            leaf
            for child in expression["conditions"]
            for leaf in _condition_leaves(child)
        ]
    return [expression]


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
