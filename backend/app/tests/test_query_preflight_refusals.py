"""Known unsupported query semantics should be refused before schema work."""

import asyncio
import pytest

from app.agent.nodes.generate_query import (
    UnanswerableQuestionError,
    preflight_refusals,
)
from app.agent import graph


def test_current_discount_question_keeps_relative_time_refusal():
    with pytest.raises(UnanswerableQuestionError) as info:
        preflight_refusals(
            "List the top 3 categories by total discount value offered right now."
        )
    assert "timezone and calendar" in info.value.reasoning


def test_verified_default_address_and_shop_active_have_distinct_reason():
    with pytest.raises(UnanswerableQuestionError) as info:
        preflight_refusals(
            "List shops in Services that have a verified default address "
            "and are currently active."
        )
    assert "Shop active status" in info.value.reasoning
    assert "verified-address meaning" in info.value.reasoning
    assert "date policy" not in info.value.reasoning


def test_discounted_offer_price_is_not_mistaken_for_discount_amount():
    preflight_refusals(
        "What is the average discounted offer price for shops named Juice?"
    )


@pytest.mark.parametrize(
    "question",
    [
        "show offers where I save ₹5000",
        "show offers where I save 5000 rupees",
        "show offers where I save more than 5000",
        "show offers where I save at least 3000 rs",
    ],
)
def test_money_context_for_savings_reaches_typed_metric_generation(question):
    preflight_refusals(question)


def test_plain_price_filters_are_allowed():
    preflight_refusals("show me offers under 500 rupees")


def test_save_favorite_shops_is_not_a_savings_request():
    preflight_refusals("save my favorite shops")


def test_save_number_of_favorite_shops_is_not_a_savings_request():
    preflight_refusals("save my 2 favorite shops")


def test_preflight_applies_to_original_wording_too():
    with pytest.raises(UnanswerableQuestionError, match="timezone and calendar"):
        preflight_refusals(
            "List currently valid offers.",
            "List offers available at the moment.",
        )


def test_active_offers_reach_business_rule_generation():
    preflight_refusals("How many offers are currently active?")


@pytest.mark.parametrize(
    "question",
    [
        "What percentage of reviews are 5 stars?",
        "What percent of bookings came from Tamil Nadu?",
        "What share of customers made a booking?",
        "What is the ratio of active to inactive offers?",
        "What proportion of reviews are 5 stars?",
        "what % of the reviews gave 5 stars",
    ],
)
def test_percentage_wordings_reach_metric_generation(question):
    preflight_refusals(question)


def test_graph_classification_preflight_routes_before_cache(monkeypatch):
    async def classify(_question):
        return {
            "intent": "data",
            "original_question": "List shops currently active with a verified address.",
            "rewritten_question": "List shops currently active with a verified address.",
        }

    monkeypatch.setattr(graph, "classify_and_rewrite", classify)
    state = {"user_question": "List shops currently active with a verified address."}
    asyncio.run(graph.node_classify(state))
    assert state["answerable"] is False
    assert graph.route_after_classify(state) == "return_save"
