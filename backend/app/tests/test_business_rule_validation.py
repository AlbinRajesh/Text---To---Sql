import json
from pathlib import Path

import pytest

from app.agent.nodes.business_rules import BusinessRulesError, BusinessRulesStore
from app.agent.prompts.generate_query_prompt import SYSTEM_PROMPT


RULEBOOK_PATH = (
    Path(__file__).resolve().parents[1] / "rules" / "business_rules.json"
)


def test_mysql_semantic_rulebook_loads():
    store = BusinessRulesStore(str(RULEBOOK_PATH))

    assert store.version
    assert store.find_relevant_rules(
        "How many shops have an average rating above 4.5?"
    )[0]["term"] == "shops_above_average_rating_threshold"


def test_subscription_pricing_rules_require_active_plans():
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    expected_filter = {
        "entity": "plans",
        "state": "active",
        "table": "plans",
        "column": "status",
        "value": 1,
    }

    for term in (
        "subscription_plan",
        "active_subscription_plans",
        "potential_recurring_revenue",
    ):
        rule = store.find_rules_by_terms([term])[0]
        assert rule["query_logic"]["state_filter"] == expected_filter

    recurring_rule = store.find_rules_by_terms(
        ["potential_recurring_revenue"]
    )[0]
    definition = recurring_rule["definition"].casefold()
    assert "users.status = 1" in definition
    assert "users.subscriber_status = 1" in definition
    assert "users.plan_id is not null" in definition
    assert "plans.status = 1" in definition
    assert "never calculate recurring revenue directly on the plans table alone" in definition
    assert any(
        item["term"] == "potential_recurring_revenue"
        for item in store.find_relevant_rules("What is our MRR?")
    )


@pytest.mark.parametrize(
    ("term", "question", "required_phrases"),
    [
        (
            "recent_user_signups_window",
            "How many users signed up in the 7 days ending on the most recent signup date?",
            ("max(date(created_at))", "interval 6 day", "users.created_at"),
        ),
        (
            "plan_product_limit_threshold",
            "Which plans allow for more than 5 products?",
            ("plans.product_limit", "plans.status = 1"),
        ),
        (
            "active_users_without_plan",
            "How many active users do not have a plan assigned?",
            ("users.status = 1", "users.plan_id is null", "users.plan_id = 0"),
        ),
        (
            "partner_user_count",
            "How many partners do we have?",
            ("users.user_type = 2",),
        ),
        (
            "missing_insurance_shop_audit",
            "Which shops have expired insurance documents?",
            ("shop_details.status = 1", "shop_details.insurance is null",
             "shop_details.insurance = ''"),
        ),
    ],
)
def test_domain_rulebook_terms_cover_tuning_edge_cases(
    term, question, required_phrases
):
    store = BusinessRulesStore(str(RULEBOOK_PATH))

    rule = store.find_rules_by_terms([term])[0]
    definition = rule["definition"].casefold()

    assert all(phrase in definition for phrase in required_phrases)
    assert any(
        item["term"] == term
        for item in store.find_relevant_rules(question)
    )


def test_expired_or_missing_insurance_synonyms_resolve_to_missing_path_rule():
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    term = store.find_rules_by_terms(["expired_or_missing_insurance"])[0]
    definition = term["definition"].casefold()
    expected_synonyms = {
        "expired insurance",
        "expired insurance documents",
        "missing insurance",
        "shops without insurance",
        "invalid insurance papers",
        "unverified compliance",
        "expired compliance documents",
        "shops with missing paperwork",
        "insurance validity expired",
        "document verification failed",
    }

    assert expected_synonyms <= set(term["synonyms"])
    assert "shop_details.status = 1" in definition
    assert "shop_details.insurance is null" in definition
    assert "shop_details.insurance = ''" in definition
    assert "no insurance expiration-date column" in definition
    assert term["query_logic"]["state_filter"] == {
        "entity": "shop_details",
        "state": "active",
        "table": "shop_details",
        "column": "status",
        "value": 1,
    }
    for synonym in expected_synonyms:
        assert any(
            rule["term"] == "expired_or_missing_insurance"
            for rule in store.find_relevant_rules(synonym)
        )


@pytest.mark.parametrize(
    ("term", "question", "definition_fragments"),
    [
        (
            "service_popularity",
            "Which is the most viewed service?",
            ("views.offer_id = offer.id", "offer.service_id = service.id",
             "offer.status = 1", "offer.service_id > 0"),
        ),
        (
            "dual_model_shops_intersection",
            "How many shops have both an active product offer and an active service offer?",
            ("product_id > 0", "service_id > 0", "status = 1", "group by shop_id"),
        ),
        (
            "shops_with_both_product_and_service_offers",
            "How many shops have both an active product offer and an active service offer?",
            ("canonical sql:", "sum(case when product_id > 0",
             "sum(case when service_id > 0"),
        ),
        (
            "current_month_expiring_offers",
            "Which active offers end this month?",
            ("dayofmonth(current_date())", "interval 1 month",
             "current_month_start", "next_month_start"),
        ),
        (
            "night_hour_views_filter",
            "Show me views between 2 AM and 4 AM.",
            ("hour(created_at) between 2 and 4", "timestamp"),
        ),
        (
            "fully_populated_image_shops",
            "Which shops have uploaded all 3 product images for their offers?",
            ("every offer", "product_img1", "product_img2", "product_img3",
             "anti-join"),
        ),
        (
            "shops_with_all_offers_having_all_images",
            "Every offer has 3 images",
            ("every offer", "not in", "shop_id is not null", "product_img3 is null"),
        ),
        (
            "most_viewed_categories_ranking",
            "Which shop categories get viewed the most?",
            ("views.offer_id", "offer.shop_id", "shop_details.category_id",
             "categories.id"),
        ),
    ],
)
def test_advanced_analytics_business_rules_are_retrievable(
    term, question, definition_fragments
):
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    rule = store.find_rules_by_terms([term])[0]

    assert any(
        item["term"] == term
        for item in store.find_relevant_rules(question)
    )
    definition = rule["definition"].casefold()
    assert all(fragment in definition for fragment in definition_fragments)
    if "canonical_sql_pattern" in rule:
        assert "%" not in rule["canonical_sql_pattern"]


def test_active_offer_ending_rule_includes_temporal_filters_and_synonyms():
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    rule = store.find_rules_by_terms(["current_month_expiring_offers"])[0]

    assert "active offers ending" in rule["synonyms"]
    assert "active offers ending this month" in rule["synonyms"]
    assert rule["recommended_filters"] == [
        "offer.status = 1",
        "offer.end_date >= (CURRENT_DATE() - INTERVAL (DAYOFMONTH(CURRENT_DATE()) - 1) DAY)",
        "offer.end_date < ((CURRENT_DATE() - INTERVAL (DAYOFMONTH(CURRENT_DATE()) - 1) DAY) + INTERVAL 1 MONTH)",
    ]
    assert any(
        item["term"] == "current_month_expiring_offers"
        for item in store.find_relevant_rules("active offers ending")
    )


@pytest.mark.parametrize(
    ("term", "synonyms", "question", "target_table", "definition_fragments"),
    [
        (
            "shop_page_views",
            (
                "checking out the page",
                "checking out their page",
                "visiting shop page",
                "most checked out",
                "page views",
            ),
            "How many users are checking out their page?",
            "views",
            ("views", "shop_id", "count(views.id)"),
        ),
        (
            "extreme_rating_thresholds",
            (
                "really bad rating",
                "worst rating",
                "terrible rating",
                "lowest rating",
            ),
            "Which shops have the worst rating?",
            "reviews",
            ("reviews.rating = 1", "min(reviews.rating)"),
        ),
        (
            "ride_transport_subcategories",
            ("ride", "taxi", "auto", "cab", "transport"),
            "Find the local cab services",
            None,
            ("subcategoryname in ('taxi', 'auto rickshaw')",),
        ),
    ],
)
def test_subjective_and_colloquial_terms_resolve_to_semantics(
    term, synonyms, question, target_table, definition_fragments
):
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    rule = store.find_rules_by_terms([term])[0]

    assert set(synonyms) <= set(rule["synonyms"])
    assert rule.get("target_table") == target_table
    assert all(
        fragment in rule["definition"].casefold()
        for fragment in definition_fragments
    )
    assert any(item["term"] == term for item in store.find_relevant_rules(question))


def test_latency_index_migration_covers_city_and_subcategory_paths():
    migration = (
        RULEBOOK_PATH.parents[2]
        / "migrations"
        / "20261024_add_query_latency_indexes.sql"
    ).read_text(encoding="utf-8").casefold()

    assert "on shop_address (city, is_default, shop_id)" in migration
    assert "on subcategories (subcategoryname)" in migration


def test_category_view_booking_comparison_uses_independent_subqueries():
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    rule = store.find_rules_by_terms(
        ["category_view_booking_comparison"]
    )[0]
    pattern = rule["canonical_sql_pattern"].casefold()

    assert any(
        item["term"] == "category_view_booking_comparison"
        for item in store.find_relevant_rules(
            "Compare views and bookings across categories"
        )
    )
    assert rule["target_table"] == "categories"
    assert "from views v join shop_details sd" in pattern
    assert "from bookings b join shop_details sd" in pattern
    assert pattern.count("left join (select") == 2
    assert "coalesce(v.total_views, 0)" in pattern
    assert "coalesce(b.total_bookings, 0)" in pattern
    assert "never join both child tables directly" in SYSTEM_PROMPT
    assert "cross-multiplication" in SYSTEM_PROMPT


def test_semantic_contract_rejects_invalid_threshold_pattern(tmp_path):
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    rulebook["terms"][0]["query_logic"]["semantic_contract"][
        "threshold_pattern"
    ] = r"\babove\s+(\d+(?:\.\d+)?"
    path = tmp_path / "business_rules.json"
    path.write_text(json.dumps(rulebook), encoding="utf-8")
    store = BusinessRulesStore(str(path))

    with pytest.raises(BusinessRulesError, match="Invalid threshold pattern"):
        store.find_relevant_rules("How many shops have an average rating above 4.5?")
