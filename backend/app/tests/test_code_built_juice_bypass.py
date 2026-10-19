import asyncio

import pytest

from app.agent.nodes import generate_query as gq
from app.agent.nodes import grade_sample as gs

SCHEMA = {
    "offer": [{"column": "id", "data_type": "bigint", "key": "PRI"}],
    "shop_details": [{"column": "id", "data_type": "int", "key": "PRI"}],
    "reviews": [{"column": "id", "data_type": "bigint", "key": "PRI"}],
}
RELATIONSHIPS = [
    {
        "constraint_name": "reviewed_offer_shop_id",
        "table": "offer",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_reviews_shop_id",
        "table": "reviews",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
]


def test_is_cheap_juice_request_true():
    assert gq.is_cheap_juice_request(("any cheap juice places in Nagercoil?", ""))
    # The rewrite dropped "cheap" but the original still has it.
    assert gq.is_cheap_juice_request(
        ("Show shops whose subcategory is 'Juice places' from Nagercoil",
         "any cheap juice places in Nagercoil?")
    )


def test_is_cheap_juice_request_false():
    assert not gq.is_cheap_juice_request(("juice places in Nagercoil", ""))
    assert not gq.is_cheap_juice_request(("any cheap juice places", ""))
    assert not gq.is_cheap_juice_request(("list shops in Nagercoil", ""))


def test_model_check_skipped_for_code_built_juice(monkeypatch):
    monkeypatch.setattr(gs, "get_cached_schema", lambda: SCHEMA)
    monkeypatch.setattr(gs, "get_cached_relationships", lambda: RELATIONSHIPS)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("the AI schema check should have been skipped")

    monkeypatch.setattr(gs, "retrieve_relevant_tables", must_not_run)
    monkeypatch.setattr(gs, "_matched_business_rules", lambda question: [])
    monkeypatch.setattr(gs, "_ask_cheap_model_if_sufficient", must_not_run)

    result = asyncio.run(
        gs.grade_schema_sufficiency(
            "Show shops whose subcategory is 'Juice places' from Nagercoil",
            "any cheap juice places in Nagercoil?",
        )
    )
    assert result["sufficient"] is True
    assert set(result["schema"]) == {"offer", "shop_details"}
    assert [e["constraint_name"] for e in result["relationships"]] == [
        "reviewed_offer_shop_id"
    ]


def test_normal_path_when_not_juice_or_when_pieces_missing():
    questions = ("any cheap juice places in Nagercoil?", "")
    assert gs._code_built_juice_context(("how many shops", ""), SCHEMA, RELATIONSHIPS) is None
    assert gs._code_built_juice_context(questions, SCHEMA, []) is None
    no_offer = {k: v for k, v in SCHEMA.items() if k != "offer"}
    assert gs._code_built_juice_context(questions, no_offer, RELATIONSHIPS) is None


RANKED_QUESTIONS = ("best rated place in Kazhakkoottam", "")


def test_is_ranked_rating_request():
    assert gq.is_ranked_rating_request(RANKED_QUESTIONS)
    assert gq.is_ranked_rating_request(("best rated shop in Nagercoil",))
    assert not gq.is_ranked_rating_request(
        ("how many best rated shops in Nagercoil", "")
    )
    assert not gq.is_ranked_rating_request(
        ("What is the full address of the highest-rated shop in Kazhakkoottam?", "")
    )
    assert not gq.is_ranked_rating_request(
        ("Show me the products available at the highest-rated restaurant in Nagercoil.", "")
    )
    assert not gq.is_ranked_rating_request(("best rated place", ""))
    assert not gq.is_ranked_rating_request(("list shops in Nagercoil", ""))


def test_model_check_skipped_for_code_built_ranked(monkeypatch):
    monkeypatch.setattr(gs, "get_cached_schema", lambda: SCHEMA)
    monkeypatch.setattr(gs, "get_cached_relationships", lambda: RELATIONSHIPS)
    monkeypatch.setattr(gs, "_matched_business_rules", lambda question: [])

    async def must_not_run(*args, **kwargs):
        raise AssertionError("the AI schema check should have been skipped")

    monkeypatch.setattr(gs, "retrieve_relevant_tables", must_not_run)
    monkeypatch.setattr(gs, "_ask_cheap_model_if_sufficient", must_not_run)

    result = asyncio.run(gs.grade_schema_sufficiency(*RANKED_QUESTIONS))
    assert result["sufficient"] is True
    assert set(result["schema"]) == {"reviews", "shop_details"}
    assert [e["constraint_name"] for e in result["relationships"]] == [
        "reviewed_reviews_shop_id"
    ]


def test_model_check_is_not_skipped_for_extra_ranked_output(monkeypatch):
    monkeypatch.setattr(gs, "get_cached_schema", lambda: SCHEMA)
    monkeypatch.setattr(gs, "get_cached_relationships", lambda: RELATIONSHIPS)

    retrieved = []

    async def fake_retrieve(question, top_k=6):
        retrieved.append(question)
        return ["shop_details", "reviews"]

    async def fake_grade(original, rewrite, schema_text, relationships):
        assert original == "What is the full address of the highest-rated shop in Kazhakkoottam?"
        assert rewrite == original
        return True, []

    monkeypatch.setattr(gs, "retrieve_relevant_tables", fake_retrieve)
    monkeypatch.setattr(gs, "_matched_business_rules", lambda question: [])
    monkeypatch.setattr(gs, "_expand_schema_for_relationships", lambda schema, tables, edges, question: (SCHEMA, RELATIONSHIPS))
    monkeypatch.setattr(gs, "_flatten_schema_for_prompt", lambda schema: "schema")
    monkeypatch.setattr(gs, "_ask_cheap_model_if_sufficient", fake_grade)

    question = "What is the full address of the highest-rated shop in Kazhakkoottam?"
    result = asyncio.run(gs.grade_schema_sufficiency(question, question))

    assert result["sufficient"] is True
    assert retrieved == [question]


def test_business_rule_contract_blocks_bypass(monkeypatch):
    rule = {"query_logic": {"semantic_contract": {"kind": "example"}}}
    monkeypatch.setattr(gs, "_matched_business_rules", lambda question: [rule])
    juice = ("any cheap juice places in Nagercoil?", "")
    assert gs._code_built_context(RANKED_QUESTIONS, SCHEMA, RELATIONSHIPS) is None
    assert gs._code_built_context(juice, SCHEMA, RELATIONSHIPS) is None


def test_failed_rule_lookup_uses_normal_check(monkeypatch):
    monkeypatch.setattr(gs, "_matched_business_rules", lambda question: None)
    assert gs._code_built_context(RANKED_QUESTIONS, SCHEMA, RELATIONSHIPS) is None


def test_no_matching_rules_allows_bypass(monkeypatch):
    monkeypatch.setattr(gs, "_matched_business_rules", lambda question: [])
    context = gs._code_built_context(RANKED_QUESTIONS, SCHEMA, RELATIONSHIPS)
    assert set(context[0]) == {"reviews", "shop_details"}
