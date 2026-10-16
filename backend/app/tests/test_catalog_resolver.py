import asyncio

import pytest

from app.agent import catalog_resolver as resolver
from app.agent import graph
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _validate_llm_output,
)
from app.agent.prompts.generate_query_prompt import build_generate_query_messages
from app.tests.test_join_queries import RELATIONSHIPS, SCHEMA, _category_count_query


CATALOG = [
    {"source_table": "categories", "value": "Services"},
    {"source_table": "categories", "value": "Beverages"},
    {"source_table": "subcategories", "value": "Beverages"},
    {"source_table": "subcategories", "value": "Restaurent"},
    {"source_table": "subcategories", "value": "Juice"},
    {"source_table": "subcategories", "value": "Snacks & Beverages"},
    {"source_table": "subcategories", "value": "Drinks & Beverages"},
    {"source_table": "subcategories", "value": "Transportation Services"},
]


@pytest.fixture(autouse=True)
def catalog_index(monkeypatch):
    monkeypatch.setattr(resolver, "get_business_value_catalog", lambda: CATALOG)

    def candidates(term, *, limit_per_field=5):
        normalized = term.casefold()
        if normalized in {"restaurant", "restaurants", "restaurent", "food places", "places to eat", "dining", "dining option", "dining options"}:
            items = [CATALOG[3]]
        elif normalized in {"drink", "drinks"}:
            items = [CATALOG[6]]
        elif normalized in {"snack", "snacks"}:
            items = [CATALOG[5]]
        elif normalized in {"service", "services"}:
            items = [CATALOG[0], CATALOG[7]]
        else:
            items = []
        return [
            {
                "source_table": item["source_table"],
                "value": item["value"],
                "exact_match": (
                    item["value"].casefold() == normalized
                    or normalized.rstrip("s") == item["value"].casefold().rstrip("s")
                ),
                "matched_token_count": 1,
                "score": 1.0,
            }
            for item in items
        ]

    monkeypatch.setattr(resolver, "resolve_value_candidates", candidates)


def test_resolves_business_types_and_exclusions():
    result = resolver.resolve_business_request(
        "Find service businesses in Nagercoil, but exclude transportation services."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "category", "value": "Services", "exclude": False},
        {
            "level": "subcategory",
            "value": "Transportation Services",
            "exclude": True,
        },
    ]


def test_and_between_same_level_business_types_requests_clarification():
    result = resolver.resolve_business_request(
        "Show restaurants and juice shops in Nagercoil."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "A shop belongs to one subcategory" in result["clarification_question"]
    assert "Did you mean shops in either one?" in result["clarification_question"]


def test_classified_under_resolves_both_catalog_values_as_or(monkeypatch):
    catalog = [
        {"source_table": "subcategories", "value": "Snacks & Beverages"},
        {"source_table": "subcategories", "value": "Drinks & Beverages"},
    ]
    monkeypatch.setattr(resolver, "get_business_value_catalog", lambda: catalog)

    result = resolver.resolve_business_request(
        "List active shops classified under Snacks & Beverages or Drinks & Beverages."
    )

    assert result["intent"] == "data"
    assert result["clarification_question"] == ""
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Snacks & Beverages", "exclude": False},
        {"level": "subcategory", "value": "Drinks & Beverages", "exclude": False},
    ]
    assert result["catalog_filter_combiner"] == "OR"


def test_selling_snacks_or_drinks_uses_or_connector():
    result = resolver.resolve_business_request(
        "Show shops that sell snacks or drinks."
    )

    assert result["intent"] == "data"
    assert {item["value"] for item in result["catalog_filters"]} == {
        "Snacks & Beverages",
        "Drinks & Beverages",
    }
    assert result["catalog_filter_combiner"] == "OR"


def test_or_request_with_unknown_item_returns_grounded_partial_filters():
    result = resolver.resolve_business_request(
        "Show shops that sell snacks or spaceships."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Snacks & Beverages", "exclude": False}
    ]
    assert result["catalog_filter_combiner"] == "OR"
    assert result["unresolved_catalog_items"] == ["spaceships"]


def test_or_request_with_only_unknown_items_keeps_clarification():
    result = resolver.resolve_business_request(
        "Show shops that sell spaceships or unicorns."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "couldn't find" in result["clarification_question"]


def test_and_request_with_unknown_item_never_returns_partial():
    result = resolver.resolve_business_request(
        "Show shops that sell snacks and spaceships."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "spaceship" in result["clarification_question"]


def test_exclusion_or_request_never_returns_partial():
    result = resolver.resolve_business_request(
        "Show shops that sell snacks or spaceships, but exclude Drinks & Beverages."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "unresolved_catalog_items" not in result


def test_or_request_with_near_spelling_asks_instead_of_dropping_typo():
    result = resolver.resolve_business_request(
        "Show shops that sell restaruent or drinks."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "restaruent" in result["clarification_question"]


def test_mixed_connectors_request_clarification():
    result = resolver.resolve_business_request(
        "Show shops classified under Snacks & Beverages or Drinks & Beverages "
        "and Restaurent."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "say whether you mean" in result["clarification_question"]


def test_mixed_connectors_take_precedence_over_unresolved_sold_item():
    result = resolver.resolve_business_request(
        "Show shops that sell snacks or drinks and juice."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "say whether you mean" in result["clarification_question"]


def test_comma_only_between_filters_requests_explicit_connector():
    result = resolver.resolve_business_request(
        "Show shops classified under Snacks & Beverages, Drinks & Beverages."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "say whether you mean" in result["clarification_question"]


def test_same_level_and_filter_requests_either_or_rephrase():
    result = resolver.resolve_business_request(
        "Show shops classified under Snacks & Beverages and Drinks & Beverages."
    )

    assert result["intent"] == "clarification"
    assert result["catalog_filters"] == []
    assert "A shop belongs to one subcategory" in result["clarification_question"]
    assert result["clarification_options"] == [
        "Shops in either one (or)",
        "Rephrase the request",
    ]


def test_comparison_request_returns_clear_refusal_before_query(monkeypatch):
    monkeypatch_catalog = [
        {"source_table": "subcategories", "value": "Snacks & Beverages"},
        {"source_table": "subcategories", "value": "Drinks & Beverages"},
    ]
    monkeypatch.setattr(
        resolver, "get_business_value_catalog", lambda: monkeypatch_catalog
    )

    result = resolver.resolve_business_request("Compare snacks vs drinks.")

    assert result["intent"] == "clarification"
    assert result["clarification_question"] == (
        "I can't compare categories in one answer yet. Please ask for each "
        "one separately."
    )


def test_misspelled_shop_type_still_clarifies():
    result = resolver.resolve_business_request("restaruent shops")

    assert result["intent"] == "clarification"
    assert "restaruent" in result["clarification_question"]


def test_unknown_shop_type_before_resolved_catalog_match_is_not_dropped(monkeypatch):
    catalog = [
        {"source_table": "subcategories", "value": "Drinks & Beverages"},
    ]
    monkeypatch.setattr(resolver, "get_business_value_catalog", lambda: catalog)

    result = resolver.resolve_business_request(
        "Show shops zzzfoo in Drinks & Beverages."
    )

    assert result["intent"] == "clarification"
    assert "zzzfoo" in result["clarification_question"]


def test_requested_output_columns_are_not_mistaken_for_business_filters():
    result = resolver.resolve_business_request(
        "List shops in Nagercoil for drinks, and include their city and category."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Drinks & Beverages", "exclude": False}
    ]


def test_unrecognized_type_in_a_comparison_is_clarified():
    result = resolver.resolve_business_request(
        "Compare the number of food-related shops and service shops in Nagercoil."
    )

    assert result["intent"] == "clarification"
    assert "food" in result["clarification_question"]


def test_clarifies_when_catalog_name_exists_at_both_levels():
    result = resolver.resolve_business_request("List Beverages in Nagercoil.")

    assert result["intent"] == "clarification"
    assert result["clarification_options"] == [
        "Category: Beverages",
        "Subcategory: Beverages",
    ]


def test_clarification_answer_selects_the_requested_catalog_level():
    result = resolver.resolve_business_request(
        "List Beverages in Nagercoil.\n"
        "Clarification: The user selected: Category: Beverages."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "category", "value": "Beverages", "exclude": False}
    ]


def test_both_clarification_answer_keeps_both_typed_values():
    result = resolver.resolve_business_request(
        "List Beverages in Nagercoil.\n"
        "Clarification: The user selected: both."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "category", "value": "Beverages", "exclude": False},
        {"level": "subcategory", "value": "Beverages", "exclude": False},
    ]
    assert result["catalog_filter_combiner"] == "OR"


def test_clarification_answer_can_ground_an_unrecognized_shop_type():
    result = resolver.resolve_business_request(
        "List flower shops in Nagercoil.\n"
        "Clarification: The user selected: Juice."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Juice", "exclude": False}
    ]


def test_unknown_inventory_item_is_reported_for_or_partial():
    result = resolver.resolve_business_request(
        "List shops in Nagercoil that sell spaceships or drinks."
    )

    assert result["intent"] == "data"
    assert result["catalog_filter_combiner"] == "OR"
    assert result["unresolved_catalog_items"] == ["spaceships"]


def test_unverified_service_type_clarifies_without_treating_it_as_inventory():
    result = resolver.resolve_business_request(
        "Find shops near Nagercoil that provide beauty or personal-care services."
    )

    assert result["intent"] == "clarification"
    assert "beauty" in result["clarification_question"]
    assert "does not verify" not in result["clarification_question"]


def test_verified_service_types_do_not_fall_back_to_the_broad_services_category(
    monkeypatch,
):
    catalog = [
        *CATALOG,
        {"source_table": "subcategories", "value": "Beauty"},
        {"source_table": "subcategories", "value": "Personal Care"},
    ]
    monkeypatch.setattr(resolver, "get_business_value_catalog", lambda: catalog)

    result = resolver.resolve_business_request(
        "Find shops near Nagercoil that provide beauty or personal-care services."
    )

    assert result["intent"] == "data"
    assert result["catalog_filters"] == [
        {"level": "subcategory", "value": "Beauty", "exclude": False},
        {"level": "subcategory", "value": "Personal Care", "exclude": False},
    ]
    assert result["catalog_filter_combiner"] == "OR"


def test_unrelated_question_does_not_require_business_catalog(monkeypatch):
    def unavailable_catalog():
        raise AssertionError("Catalog access should be skipped for unrelated requests.")

    monkeypatch.setattr(resolver, "get_business_value_catalog", unavailable_catalog)

    assert resolver.resolve_business_request(
        "What is the total number of active shops?"
    ) is None


def test_unknown_business_noun_is_clarified_instead_of_broadly_searched():
    result = resolver.resolve_business_request("Find florists in Nagercoil.")

    assert result["intent"] == "clarification"
    assert "florist" in result["clarification_question"]


def test_validator_rejects_wrong_catalog_level():
    query = {
        "where_expression": {
            "table": "subcategories",
            "column": "subcategoryname",
            "operator": "=",
            "value": "Beverages",
        },
        "joins": [],
    }

    with pytest.raises(ValueError, match="wrong catalog level"):
        resolver.validate_catalog_filter_plan(
            query,
            [{"level": "category", "value": "Beverages", "exclude": False}],
        )


def test_validator_accepts_nested_or_for_compound_filters():
    query = {
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {"table": "shop_address", "column": "city", "operator": "=", "value": "Nagercoil"},
                {
                    "boolean": "OR",
                    "conditions": [
                        {
                            "table": "subcategories",
                            "column": "subcategoryname",
                            "operator": "=",
                            "value": "Restaurent",
                        },
                        {
                            "boolean": "OR",
                            "conditions": [
                                {
                                    "table": "subcategories",
                                    "column": "subcategoryname",
                                    "operator": "=",
                                    "value": "Juice",
                                }
                            ],
                        },
                    ],
                },
            ],
        },
        "joins": [],
    }

    resolver.validate_catalog_filter_plan(
        query,
        [
            {"level": "subcategory", "value": "Restaurent", "exclude": False},
            {"level": "subcategory", "value": "Juice", "exclude": False},
        ],
    )


def test_validator_rejects_unrequested_catalog_filter():
    query = {
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "categories",
                    "column": "categoriesname",
                    "operator": "=",
                    "value": "Services",
                },
                {
                    "table": "subcategories",
                    "column": "subcategoryname",
                    "operator": "=",
                    "value": "Juice",
                },
            ],
        },
        "joins": [],
    }

    with pytest.raises(ValueError, match="unrequested"):
        resolver.validate_catalog_filter_plan(
            query,
            [{"level": "category", "value": "Services", "exclude": False}],
        )


def test_validator_requires_multiple_requested_types_to_remain_or():
    query = {
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "subcategories",
                    "column": "subcategoryname",
                    "operator": "=",
                    "value": "Restaurent",
                },
                {
                    "table": "subcategories",
                    "column": "subcategoryname",
                    "operator": "=",
                    "value": "Juice",
                },
            ],
        },
        "joins": [],
    }

    with pytest.raises(ValueError, match="OR filter"):
        resolver.validate_catalog_filter_plan(
            query,
            [
                {"level": "subcategory", "value": "Restaurent", "exclude": False},
                {"level": "subcategory", "value": "Juice", "exclude": False},
            ],
        )


def test_structured_plan_validation_enforces_typed_filter_level():
    plan = _category_count_query()
    plan["where_conditions"] = [
        {
            "table": "categories",
            "column": "categoriesname",
            "operator": "=",
            "value": "Beverages",
        }
    ]

    with pytest.raises(QueryGenerationError, match="wrong catalog level"):
        _validate_llm_output(
            plan,
            SCHEMA,
            RELATIONSHIPS,
            question="List Beverages in Nagercoil.",
            matched_business_rules=[],
            catalog_filters=[
                {"level": "subcategory", "value": "Beverages", "exclude": False}
            ],
        )


def test_generation_prompt_receives_typed_filter_levels_and_combiner():
    messages = build_generate_query_messages(
        "Show restaurants and juice shops in Nagercoil.",
        {"shop_details": [{"column": "id"}]},
        [],
        catalog_filters=[
            {"level": "subcategory", "value": "Restaurent", "exclude": False},
            {"level": "subcategory", "value": "Juice", "exclude": False},
        ],
        catalog_filter_combiner="OR",
    )

    assert '"verified_business_type_filter_combiner": "OR"' in messages[1]["content"]
    assert '"level": "subcategory"' in messages[1]["content"]


def test_graph_passes_and_revalidates_typed_filters(monkeypatch):
    expected_filters = [
        {"level": "subcategory", "value": "Juice", "exclude": False}
    ]
    captured = {}

    async def generate(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "where_expression": {
                "table": "subcategories",
                "column": "subcategoryname",
                "operator": "=",
                "value": "Juice",
            },
            "joins": [],
        }

    validated = []
    monkeypatch.setattr(graph, "generate_query", generate)
    monkeypatch.setattr(
        graph,
        "validate_catalog_filter_plan",
        lambda query, filters: validated.append((query, filters)),
    )
    state = {
        "classifier_result": {
            "original_question": "Find juice shops.",
            "rewritten_question": "Find juice shops.",
            "catalog_filters": expected_filters,
            "catalog_filter_combiner": "AND",
        },
        "schema_context": {},
        "matched_business_rules": [],
    }

    asyncio.run(graph.node_generate_query(state))

    assert captured["catalog_filters"] == expected_filters
    assert captured["catalog_filter_combiner"] == "AND"
    assert validated == [(state["generated_query"], expected_filters)]
