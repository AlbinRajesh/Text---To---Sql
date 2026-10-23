"""Known unsupported query semantics should be refused before schema work."""

import asyncio
import pytest

from app.agent.nodes.generate_query import (
    UnanswerableQuestionError,
    preflight_refusals,
)
from app.agent import graph


def test_current_discount_question_is_not_refused_by_date_wording():
    preflight_refusals(
        "List the top 3 categories by total discount value offered right now."
    )


def test_preflight_blocks_destructive_sql_injection():
    with pytest.raises(UnanswerableQuestionError, match="not permitted"):
        preflight_refusals("List shops; DROP TABLE users")


def test_schema_semantics_are_not_refused_by_keyword_preflight():
    preflight_refusals(
        "List shops in Services that have a verified default address "
        "and are currently active."
    )


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


def test_preflight_does_not_refuse_current_state_wording():
    preflight_refusals(
        "List currently valid offers.",
        "List offers available at the moment.",
    )


def test_currently_active_shop_question_is_not_refused_by_preflight():
    preflight_refusals("List shops that are currently active.")


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


def test_graph_classification_routes_semantic_questions_to_schema_pipeline(monkeypatch):
    async def classify(_question, *_args, **_kwargs):
        return {
            "intent": "data",
            "original_question": "List shops currently active with a verified address.",
            "rewritten_question": "List shops currently active with a verified address.",
        }

    monkeypatch.setattr(graph, "classify_and_rewrite", classify)
    state = {"user_question": "List shops currently active with a verified address."}
    asyncio.run(graph.node_classify(state))
    assert state.get("answerable") is not False
    assert graph.route_after_classify(state) == "cache_check"
