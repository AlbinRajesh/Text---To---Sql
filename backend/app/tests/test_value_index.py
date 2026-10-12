import asyncio

import pytest

from app.services import value_index


@pytest.fixture(autouse=True)
def reset_value_index():
    old_values = value_index._values
    old_version = value_index._version
    value_index._values = []
    value_index._version = None
    yield
    value_index._values = old_values
    value_index._version = old_version


def test_index_resolves_real_shop_city_and_misspelled_subcategory(monkeypatch):
    rows = [
        ("shop_details", "name", 105, "A1 Juice house"),
        ("shop_details", "name", 117, "kINGSTAR FRESH JUICE POINT"),
        ("shop_details", "name", 130, "Popular juice corner"),
        ("shop_address", "city", None, "Nagercoil"),
        ("subcategories", "subcategoryname", 4, "Restaurent"),
    ]

    async def fake_fetch_all_readonly(query):
        assert query == value_index.VALUE_CATALOG_SQL
        return rows

    monkeypatch.setattr(value_index, "fetch_all_readonly", fake_fetch_all_readonly)
    asyncio.run(value_index.build_value_index())

    kingstar = value_index.resolve_value_candidates("kingstar juice phone number")
    shop_names = [
        item["value"]
        for item in kingstar
        if item["source_table"] == "shop_details"
    ]
    assert shop_names[0] == "kINGSTAR FRESH JUICE POINT"
    assert kingstar[0]["source_id"] == 117
    assert kingstar[0]["unique_match"] is True

    nagercoil = value_index.resolve_value_candidates("cheap juice places in Nagercoil")
    assert any(
        item["source_column"] == "city" and item["value"] == "Nagercoil"
        for item in nagercoil
    )

    restaurant = value_index.resolve_value_candidates("good restaurant near Technopark")
    assert any(
        item["source_table"] == "subcategories"
        and item["value"] == "Restaurent"
        for item in restaurant
    )


def test_candidate_signature_changes_when_candidates_change():
    first = [{
        "source_table": "shop_details",
        "source_column": "name",
        "source_id": 117,
        "value": "kINGSTAR FRESH JUICE POINT",
        "matched_terms": ["kingstar", "juice"],
        "unique_match": True,
    }]
    second = [{
        **first[0],
        "value": "A1 Juice house",
    }]

    assert value_index.value_signature(first) != value_index.value_signature(second)


def test_assumptions_only_describe_validated_semantics():
    question = "cheap juice places near Nagercoil"
    candidates = [{
        "source_table": "shop_address",
        "source_column": "city",
        "value": "Nagercoil",
        "matched_terms": ["nagercoil"],
    }]
    query = {
        "table": "offer",
        "joins": [{"table": "shop_address"}],
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "shop_address",
                    "column": "city",
                    "operator": "LIKE",
                    "value": "%Nagercoil%",
                }
            ],
        },
        "select_items": [{
            "table": "offer",
            "column": "offer_price",
            "aggregate": "MIN",
        }],
        "order_by": [{"column": "offer_price", "direction": "ASC"}],
        "having_expression": None,
    }

    notes = value_index.derive_interpretation_notes(question, query, candidates)

    assert any("Nagercoil" in note for note in notes)
    assert any("lowest listed offer price" in note for note in notes)
    assert any("not a geographic-distance calculation" in note for note in notes)

    query["order_by"] = []
    assert not any(
        "I interpreted 'cheap'" in note
        for note in value_index.derive_interpretation_notes(question, query, candidates)
    )


def test_assumption_does_not_claim_individual_offer_candidates_for_broad_like():
    query = {
        "table": "offer",
        "joins": [{"table": "shop_details"}],
        "where_expression": {
            "table": "offer",
            "column": "offer_heading",
            "operator": "LIKE",
            "value": "%juice%",
        },
        "select_items": [{"table": "offer", "column": "offer_heading"}],
    }
    candidates = [{
        "source_table": "offer",
        "source_column": "offer_heading",
        "source_id": 51,
        "value": "fresh juice special offer",
        "matched_terms": ["juice"],
    }]

    notes = value_index.derive_interpretation_notes(
        "show juice offers", query, candidates
    )
    assert not any("fresh juice special offer" in note for note in notes)
