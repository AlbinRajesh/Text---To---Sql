import pytest

from app.agent import catalog_resolver
from app.services import clarification_gate


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
    return values


@pytest.mark.xfail(
    strict=True,
    reason="Resolver ask-gate integration is scheduled for Phase 2b.",
)
def test_top_five_category_question_does_not_ask_about_numeric_filler(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show the top 5 categories by number of shops."
    )

    assert result is None or result["intent"] != "clarification"


@pytest.mark.xfail(
    strict=True,
    reason="Resolver ask-gate integration is scheduled for Phase 2b.",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="Resolver ask-gate integration is scheduled for Phase 2b.",
)
def test_food_related_uses_normalized_phrase_when_catalog_has_no_candidate(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show food-related shops in Nagercoil."
    )

    assert result["intent"] == "clarification"
    assert "couldn't find a shop type like 'food'" in (
        result["clarification_question"].casefold()
    )
    assert "food related" not in result["clarification_question"].casefold()
    assert result["clarification_options"] == []


@pytest.mark.xfail(
    strict=True,
    reason="Placeholder filtering is scheduled for Phase 2b.",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="Explicit resolver clarification kinds are scheduled for Phase 2c.",
)
def test_resolver_clarification_declares_its_selection_kind(monkeypatch):
    values = [
        {"source_table": "categories", "value": "Food"},
        {"source_table": "categories", "value": "Food & Dining"},
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
        "Show Food shops in Nagercoil."
    )

    assert result["intent"] == "clarification"
    assert result["clarification_kind"] == "choose_one"


@pytest.mark.xfail(
    strict=True,
    reason="Asked-item propagation on clarification resume is scheduled for Phase 2c.",
)
def test_resumed_request_does_not_ask_about_the_same_item_again(catalog):
    result = catalog_resolver.resolve_business_request(
        "Show drinks-related shops in Nagercoil.",
        asked_items=["drinks"],
    )

    assert result["intent"] == "refusal"
    assert not result.get("clarification_question")
