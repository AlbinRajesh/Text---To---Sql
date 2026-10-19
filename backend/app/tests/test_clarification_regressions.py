import asyncio

import pytest

from app.agent import catalog_resolver
from app.agent.nodes import classifier_rewrite
from app.api import chat
from app.services import clarification_gate
from app.services import pending_interactions as pending_store


@pytest.fixture(autouse=True)
def memory_pending_store(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: None,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())
    pending_store.clear_all_pending_interactions()
    yield
    asyncio.run(pending_store.close_pending_interaction_store())


@pytest.fixture
def catalog(monkeypatch):
    values = [
        {"source_table": "categories", "value": "Food & Dining"},
        {"source_table": "categories", "value": "Sports Products"},
        {"source_table": "subcategories", "value": "Drinks & Beverages"},
        {"source_table": "subcategories", "value": "All"},
        {"source_table": "subcategories", "value": "More"},
    ]
    monkeypatch.setattr(
        catalog_resolver,
        "get_business_value_catalog",
        lambda: values,
    )

    def resolve_candidates(term, *, limit_per_field=5):
        normalized = term.casefold()
        matches = {
            "food": values[0],
            "drink": values[2],
            "drinks": values[2],
        }
        value = matches.get(normalized)
        if value is None:
            return []
        return [{
            "source_table": value["source_table"],
            "value": value["value"],
            "exact_match": False,
            "matched_token_count": 1,
            "score": 1.0,
        }]

    monkeypatch.setattr(
        catalog_resolver,
        "resolve_value_candidates",
        resolve_candidates,
    )
    monkeypatch.setattr(
        catalog_resolver,
        "shop_name_matches_term",
        lambda _term: False,
    )
    return values


def test_top_five_category_question_does_not_ask_about_numeric_filler(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show the top 5 categories by number of shops."
    )

    assert result is None or result["intent"] != "clarification"


def test_drinks_related_resolves_the_normalized_catalog_term(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show drinks-related shops in Nagercoil."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {
            "level": "subcategory",
            "value": "Drinks & Beverages",
            "exclude": False,
        }
    ]


def test_food_related_resolves_matching_food_and_dining_category(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show food-related shops in Nagercoil."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {
            "level": "category",
            "value": "Food & Dining",
            "exclude": False,
        }
    ]
    assert not result["clarification_question"]


def test_unmatched_normalized_phrase_uses_plain_not_found_message(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show xyz-related shops in Nagercoil."
    )

    assert result["intent"] == "clarification"
    assert result["clarification_question"] == (
        "I couldn't find a shop type like 'xyz'."
    )
    assert result["clarification_options"] == []


def test_unmatched_type_matching_shop_name_offers_name_search(catalog, monkeypatch):
    monkeypatch.setattr(
        catalog_resolver,
        "shop_name_matches_term",
        lambda term: term in {"juice", "bakery"},
    )

    for term in ("juice", "bakery"):
        result = catalog_resolver.resolve_business_request(
            f"Show {term} shops in Nagercoil."
        )

        expected_label = f"Search shop names for '{term}'"
        assert result["intent"] == "clarification"
        assert result["clarification_kind"] == "choose_one"
        assert result["clarification_options"] == [expected_label]
        assert result["clarification_option_records"] == [{
            "label": expected_label,
            "kind": "name_search",
            "term": term,
        }]


def test_placeholder_catalog_values_are_not_matched_or_offered(catalog):
    phrases = catalog_resolver._catalog_phrases()
    options = catalog_resolver._clarification_options([
        catalog_resolver.CatalogValue("subcategory", "All"),
        catalog_resolver.CatalogValue("subcategory", "More"),
        catalog_resolver.CatalogValue("subcategory", "Drinks & Beverages"),
    ])

    assert all(
        "all" not in tokens and "more" not in tokens
        for tokens in phrases
    )
    assert options == ["Drinks & Beverages"]
    assert clarification_gate.is_placeholder("All")
    assert clarification_gate.is_placeholder("More")


def test_resolver_clarification_declares_its_selection_kind(monkeypatch):
    values = [
        {"source_table": "categories", "value": "Fitness"},
        {"source_table": "subcategories", "value": "Fitness"},
    ]
    monkeypatch.setattr(
        catalog_resolver,
        "get_business_value_catalog",
        lambda: values,
    )
    monkeypatch.setattr(
        catalog_resolver,
        "resolve_value_candidates",
        lambda *_args, **_kwargs: [],
    )

    result = catalog_resolver.resolve_business_request(
        "Show Fitness shops in Nagercoil."
    )

    assert result["intent"] == "clarification"
    assert result["clarification_kind"] == "choose_one"


def test_resumed_request_does_not_ask_about_the_same_item_again(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show drinks-related shops in Nagercoil.",
        asked_items=["drinks"],
    )

    assert result["intent"] == "chat"
    assert not result.get("clarification_question")
    assert "rephrase" in result["chat_reply"]


def test_yes_with_missing_classifier_filter_reprompts_without_pipeline(monkeypatch):
    conversation_id = "e6a3bb54-c115-4f9d-8a09-ef227a265581"
    pipeline_calls = []

    async def async_none(_conversation_id):
        return None

    monkeypatch.setattr(chat, "clear_suggestion_plans", async_none)
    monkeypatch.setattr(
        classifier_rewrite,
        "get_business_value_catalog",
        lambda: [],
    )

    async def fake_run_pipeline(
        question,
        request_id=None,
        conversation_id=None,
        asked_items=None,
    ):
        pipeline_calls.append((question, asked_items))
        if len(pipeline_calls) == 1:
            return {
                "user_question": question,
                "final_answer": {
                    "clarification_question": (
                        "Do you mean 'shops' in Nagercoil?"
                    ),
                    "clarification_options": [],
                },
            }

        result = classifier_rewrite._gate_classifier_clarification(
            {
                "intent": "clarification",
                "original_question": question,
                "clarification_question": (
                    "Do you mean 'shops' in Nagercoil?"
                ),
                "clarification_options": [],
            },
            asked_items or [],
        )
        return {
            "user_question": question,
            "final_answer": {
                "chat_reply": result["chat_reply"],
            },
        }

    monkeypatch.setattr(chat, "run_pipeline", fake_run_pipeline)

    first = asyncio.run(chat.chat(chat.ChatRequest(
        question="How many shops are in Nagercoil?",
        conversation_id=conversation_id,
    )))
    assert first.status == "clarification"

    second = asyncio.run(chat.chat(chat.ChatRequest(
        question="yes",
        conversation_id=conversation_id,
    )))
    assert second.status == "clarification"
    assert second.message == "Do you mean 'shops' in Nagercoil?"
    assert len(pipeline_calls) == 1
    pending = asyncio.run(chat.get_pending_interaction(conversation_id))
    assert pending["payload"]["invalid_replies"] == 1
