import asyncio

import pytest

from app.agent.nodes import classifier_rewrite as cr
from app.agent import catalog_resolver as catalog_resolver_module


SCHEMA_WITH_OFFER_DESCRIPTION = {
    "shop_details": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "offer": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "description", "data_type": "longtext", "key": ""},
    ],
}


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def call_agent_json(self, agent, messages, temperature=0.0):
        self.calls += 1
        return self.replies.pop(0)


def _reply(rewrite):
    return {
        "intent": "data",
        "complexity": "simple",
        "rewritten_question": rewrite,
        "chat_reply": "",
        "reasoning": "x",
    }


def _run(
    question,
    replies,
    monkeypatch,
    schema=SCHEMA_WITH_OFFER_DESCRIPTION,
    catalog_resolver=None,
):
    llm = FakeLLM(replies)
    monkeypatch.setattr(cr, "get_llm_service", lambda: llm)
    monkeypatch.setattr(cr, "get_cached_schema", lambda: schema)
    catalog_values = {
        "restaurant": ("subcategories", [("Restaurent", False)]),
        "drink": (
            "subcategories",
            [("Drinks", True), ("Drinks & Beverages", False)],
        ),
        "beverage": (
            "subcategories",
            [("Beverages", True), ("Drinks & Beverages", False)],
        ),
        "service": (
            "categories",
            [("Services", True), ("Transportation Services", False)],
        ),
    }

    def resolve_value_candidates(term, *, limit_per_field=5):
        catalog_entry = catalog_values.get(term)
        if catalog_entry is None:
            return []
        source_table, values = catalog_entry
        return [
            {
                "source_table": source_table,
                "source_column": (
                    "subcategoryname" if source_table == "subcategories"
                    else "categoriesname"
                ),
                "source_id": index,
                "value": value,
                "matched_terms": [term],
                "matched_token_count": 1,
                "score": 1.0,
                "exact_match": exact_match,
            }
            for index, (value, exact_match) in enumerate(values, start=1)
        ]

    candidates_resolver = catalog_resolver or resolve_value_candidates
    if catalog_resolver is None:
        catalog_values_for_index = [
            {
                "source_table": source_table,
                "source_column": (
                    "subcategoryname" if source_table == "subcategories"
                    else "categoriesname"
                ),
                "value": value,
            }
            for source_table, values in catalog_values.values()
            for value, _ in values
        ]
    else:
        term_by_question = (
            "drink" if "drinks" in question.casefold()
            else "beverage" if "beverage" in question.casefold()
            else "bakeries" if "bakeries" in question.casefold()
            else ""
        )
        catalog_values_for_index = [
            {
                "source_table": candidate["source_table"],
                "source_column": candidate["source_column"],
                "value": candidate["value"],
            }
            for candidate in (
                candidates_resolver(term_by_question)
                if term_by_question else []
            )
        ]

    def resolve_for_catalog(term, *, limit_per_field=5):
        if catalog_resolver is None:
            return resolve_value_candidates(term, limit_per_field=limit_per_field)
        target = (
            "drink" if "drinks" in question.casefold()
            else "beverage" if "beverage" in question.casefold()
            else "bakeries" if "bakeries" in question.casefold()
            else ""
        )
        return (
            candidates_resolver(term)
            if term == target else []
        )

    monkeypatch.setattr(
        catalog_resolver_module,
        "get_business_value_catalog",
        lambda: catalog_values_for_index,
    )
    monkeypatch.setattr(
        catalog_resolver_module,
        "resolve_value_candidates",
        resolve_for_catalog,
    )
    return asyncio.run(cr.classify_and_rewrite(question)), llm


def test_schema_field_word_added_by_rewrite_falls_back_without_retry(monkeypatch):
    bad = _reply("Show shops whose name or description includes 'fish meals'")

    result, llm = _run("who sells fish meals?", [bad], monkeypatch)

    assert result["rewritten_question"] == "who sells fish meals?"
    assert llm.calls == 1


def test_added_schema_table_word_falls_back_to_original(monkeypatch):
    bad = _reply("Search the offer for 'fish meals'")

    result, llm = _run("who sells fish meals?", [bad], monkeypatch)

    assert result["rewritten_question"] == "who sells fish meals?"
    assert llm.calls == 1


def test_schema_words_already_in_original_are_allowed(monkeypatch):
    rewrite = "Show me the description of this offer"
    result, llm = _run(
        "show me the offer description",
        [_reply(rewrite)],
        monkeypatch,
    )

    assert result["rewritten_question"] == rewrite
    assert llm.calls == 1


def test_short_schema_word_is_ignored():
    schema = {
        "shop_address": [
            {"column": "is_default", "data_type": "tinyint", "key": ""},
        ]
    }

    assert cr._find_rewrite_drift(
        "show shops", "show shops in town", schema
    ) is None
    assert "is" not in cr._schema_terms(schema)


def test_description_schema_word_is_still_detected():
    drift = cr._find_rewrite_drift(
        "who sells fish meals?",
        "Show shops with a description matching fish meals",
        SCHEMA_WITH_OFFER_DESCRIPTION,
    )

    assert drift == "added schema field or table word 'description'"


def test_location_rewritten_as_named_falls_back_without_retry(monkeypatch):
    original = "shops from Kochi"
    rewrite = "Show the shop named 'Kochi'"
    drift = cr._find_rewrite_drift(original, rewrite, SCHEMA_WITH_OFFER_DESCRIPTION)
    result, llm = _run(
        original,
        [_reply(rewrite)],
        monkeypatch,
    )

    assert drift and "named" in drift
    assert result["rewritten_question"] == original
    assert llm.calls == 1


def test_value_rewritten_as_called_falls_back_without_retry(monkeypatch):
    original = "offers in Nagercoil"
    rewrite = "Show offers called 'Nagercoil'"
    drift = cr._find_rewrite_drift(original, rewrite, SCHEMA_WITH_OFFER_DESCRIPTION)
    result, llm = _run(
        original,
        [_reply(rewrite)],
        monkeypatch,
    )

    assert drift and "named" in drift
    assert result["rewritten_question"] == original
    assert llm.calls == 1


def test_missing_schema_cache_uses_original_without_retry(monkeypatch):
    from app.agent.nodes.schema_grounding import SchemaGroundingError

    def no_schema():
        raise SchemaGroundingError("cache not initialized")

    llm = FakeLLM([_reply("Show shops with a description matching fish")])
    monkeypatch.setattr(cr, "get_llm_service", lambda: llm)
    monkeypatch.setattr(cr, "get_cached_schema", no_schema)
    monkeypatch.setattr(catalog_resolver_module, "get_business_value_catalog", lambda: [])
    monkeypatch.setattr(catalog_resolver_module, "resolve_value_candidates", lambda *_args, **_kwargs: [])

    result = asyncio.run(cr.classify_and_rewrite("who sells fish meals?"))

    assert result["rewritten_question"] == "who sells fish meals?"
    assert llm.calls == 1


@pytest.mark.parametrize(
    "question",
    [
        "What percentage of offers have a discount greater than 50 percent?",
        "Which subcategories have the most active shops in Nagercoil?",
    ],
)
def test_self_contained_aggregate_questions_are_not_left_in_clarification(
    question, monkeypatch
):
    result, _ = _run(
        question,
        [{
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": question,
            "clarification_question": "Which records do you mean?",
            "clarification_options": [],
        }],
        monkeypatch,
    )

    assert result["intent"] == "data"
    assert result["rewritten_question"] == question
    assert result["clarification_question"] == ""


def test_short_answer_on_a_self_contained_request_resumes_the_original_question(
    monkeypatch,
):
    original = "Which subcategories have the most active shops in Nagercoil?"
    continued = f"{original}\nClarification: The user selected: all."
    result, _ = _run(
        continued,
        [{
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": continued,
            "clarification_question": "Do you want names or counts?",
            "clarification_options": [],
        }],
        monkeypatch,
    )

    assert result["intent"] == "data"
    assert result["rewritten_question"] == original


@pytest.mark.parametrize(
    ("question", "expected_level", "expected_value"),
    [
        (
            "Show me drinks in Nagercoil.",
            "subcategory",
            "Drinks",
        ),
        (
            "List beverages in Nagercoil",
            "subcategory",
            "Beverages",
        ),
        (
            "Show me restaurants in Nagercoil.",
            "subcategory",
            "Restaurent",
        ),
        (
            "Show me dining options in Nagercoil.",
            "subcategory",
            "Restaurent",
        ),
        (
            "Find food places in Nagercoil.",
            "subcategory",
            "Restaurent",
        ),
        (
            "Show me services in Nagercoil.",
            "category",
            "Services",
        ),
        (
            "What service shops are in Nagercoil?",
            "category",
            "Services",
        ),
    ],
)
def test_known_shop_terms_are_resolved_even_when_classifier_asks_for_clarification(
    question, expected_level, expected_value, monkeypatch
):
    result, _ = _run(
        question,
        [{
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": question,
            "clarification_question": "Which type do you mean?",
            "clarification_options": [],
        }],
        monkeypatch,
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": expected_level, "value": expected_value, "exclude": False}
    ]
    assert result["clarification_question"] == ""


def test_ambiguous_catalog_values_are_not_guessed(monkeypatch):
    def resolve_ambiguous_drinks(term, *, limit_per_field=5):
        assert term == "drink"
        return [
            {
                "source_table": "subcategories",
                "source_column": "subcategoryname",
                "value": value,
                "matched_token_count": 1,
                "score": 1.0,
                "exact_match": False,
            }
            for value in ("Drink Shops", "Drink Bars")
        ]

    question = "Show me drinks in Nagercoil."
    result, llm = _run(
        question,
        [_reply("List shops whose name contains drinks")],
        monkeypatch,
        catalog_resolver=resolve_ambiguous_drinks,
    )

    assert result["intent"] == "clarification"
    assert result["clarification_question"] == (
        "Which category or subcategory do you mean by 'drinks'?"
    )
    assert result["clarification_options"] == ["Drink Shops", "Drink Bars"]
    assert llm.calls == 0


def test_unrecognized_business_term_uses_typed_catalog_without_alias(monkeypatch):
    question = "List bakeries in Nagercoil"

    def resolve_bakeries(term, *, limit_per_field=5):
        assert term == "bakeries"
        return [{
            "source_table": "subcategories",
            "source_column": "subcategoryname",
            "value": "Bakery",
            "matched_token_count": 1,
            "score": 1.0,
            "exact_match": False,
        }]

    result, llm = _run(
        question,
        [_reply("List shops whose name contains bakery")],
        monkeypatch,
        catalog_resolver=resolve_bakeries,
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Bakery", "exclude": False}
    ]
    assert llm.calls == 0


def test_beverages_with_multiple_catalog_matches_get_grounded_options(monkeypatch):
    question = "List beverages in Nagercoil"

    def resolve_ambiguous_beverages(term, *, limit_per_field=5):
        assert term == "beverage"
        return [
            {
                "source_table": "subcategories",
                "source_column": "subcategoryname",
                "value": value,
                "matched_token_count": 1,
                "score": 1.0,
                "exact_match": False,
            }
            for value in ("Beverage Shops", "Beverage Suppliers")
        ]

    result, llm = _run(
        question,
        [_reply("Search shop names for beverages")],
        monkeypatch,
        catalog_resolver=resolve_ambiguous_beverages,
    )

    assert result["intent"] == "clarification"
    assert result["clarification_options"] == [
        "Beverage Shops",
        "Beverage Suppliers",
    ]
    assert llm.calls == 0


def test_known_business_term_without_catalog_match_does_not_fall_through(
    monkeypatch,
):
    question = "List beverages in Nagercoil"

    def no_beverage_values(_term, *, limit_per_field=5):
        return []

    result, llm = _run(
        question,
        [_reply("Search shop names for beverages")],
        monkeypatch,
        catalog_resolver=no_beverage_values,
    )

    assert result["intent"] == "clarification"
    assert result["clarification_question"] == (
        "Which category or subcategory do you mean by 'beverages'?"
    )
    assert result["clarification_options"] == []
    assert llm.calls == 0


def test_unmatched_shop_type_does_not_fall_through_to_broad_query(monkeypatch):
    question = "List bakeries in Nagercoil"

    def no_bakery_values(_term, *, limit_per_field=5):
        return []

    result, llm = _run(
        question,
        [_reply("Search shop names for bakeries")],
        monkeypatch,
        catalog_resolver=no_bakery_values,
    )

    assert result["intent"] == "clarification"
    assert result["clarification_options"] == []
    assert result["clarification_question"] == (
        "Which category or subcategory do you mean by 'bakeries'?"
    )
    assert llm.calls == 0


def test_exact_services_category_beats_a_containing_category(monkeypatch):
    question = "List services in Nagercoil"
    result, _ = _run(
        question,
        [{
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": question,
            "clarification_question": "Which services?",
            "clarification_options": [],
        }],
        monkeypatch,
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "category", "value": "Services", "exclude": False}
    ]


def test_known_shop_clarification_selection_changes_the_filter(monkeypatch):
    question = (
        "Show me drinks in Nagercoil.\n"
        "Clarification: The user selected: resturent."
    )
    result, _ = _run(
        question,
        [{
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": question,
            "clarification_question": "Which type do you mean?",
            "clarification_options": [],
        }],
        monkeypatch,
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Drinks", "exclude": False}
    ]


def test_known_shop_term_resolution_is_not_disabled_with_question_rewriting(
    monkeypatch,
):
    monkeypatch.setenv("USE_QUESTION_REWRITE", "false")
    result, _ = _run(
        "Show me restaurants in Nagercoil.",
        [_reply("Show me restaurants in Nagercoil.")],
        monkeypatch,
    )

    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Restaurent", "exclude": False}
    ]


def test_both_shop_clarification_choices_are_combined(monkeypatch):
    question = (
        "Show me drinks in Nagercoil.\n"
        "Clarification: The user selected: resturent.\n"
        "Clarification: The user selected: both."
    )
    result, _ = _run(
        question,
        [{
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": question,
            "clarification_question": "Do you want drinks or restaurants?",
            "clarification_options": [],
        }],
        monkeypatch,
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Drinks", "exclude": False}
    ]


@pytest.mark.parametrize(
    "question",
    [
        "For each category, compare the number of active shops with the number "
        "of inactive shops",
        "For each category, compare the number of active shops with the number "
        "of inactive shops\nClarification: The user selected: All cities no "
        "city filter.",
    ],
)
def test_active_inactive_category_comparison_defaults_to_all_cities(
    question, monkeypatch
):
    result, _ = _run(
        question,
        [{
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": question,
            "clarification_question": "Which city should the comparison be based on?",
            "clarification_options": [],
        }],
        monkeypatch,
    )

    assert result["intent"] == "data"
    assert result["rewritten_question"] == question.split("\n", 1)[0]
    assert result["clarification_question"] == ""
