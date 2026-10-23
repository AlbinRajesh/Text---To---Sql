import json
from pathlib import Path

import pytest

from app.agent.nodes.business_rules import BusinessRulesError, BusinessRulesStore


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
