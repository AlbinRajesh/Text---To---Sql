"""Known unsupported query semantics should be refused before schema work."""

import asyncio
import pytest

from app.agent.nodes.generate_query import (
    UnanswerableQuestionError,
    preflight_refusals,
)
from app.agent import graph


def test_current_discount_value_has_honest_combined_refusal():
    with pytest.raises(UnanswerableQuestionError) as info:
        preflight_refusals(
            "List the top 3 categories by total discount value offered right now."
        )
    assert "date policy" in info.value.reasoning
    assert "discount value" in info.value.reasoning


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


def test_preflight_applies_to_original_wording_too():
    with pytest.raises(UnanswerableQuestionError, match="timezone and calendar"):
        preflight_refusals(
            "List currently valid offers.",
            "List offers available at the moment.",
        )


def test_currently_active_offers_keep_the_existing_refusal():
    with pytest.raises(UnanswerableQuestionError, match="active offer"):
        preflight_refusals("How many offers are currently active?")


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
