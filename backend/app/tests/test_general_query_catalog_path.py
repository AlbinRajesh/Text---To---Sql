import asyncio

import pytest

from app.agent.nodes import grade_sample
from app.agent.nodes.business_rules import find_relevant_rules
from app.agent.prompts.grader_prompt import (
    build_ranked_result_note,
    ranked_result_metadata,
)


@pytest.mark.parametrize(
    "question",
    [
        "Any cheap juice places in Nagercoil?",
        "Show the best-rated shops in Nagercoil.",
    ],
)
def test_schema_grader_uses_normal_retrieval_for_ranking_and_price_questions(
    monkeypatch, question
):
    schema = {
        "offer": [{"column": "offer_price", "key": ""}],
        "shop_details": [{"column": "id", "key": "PRI"}],
        "reviews": [{"column": "rating", "key": ""}],
    }
    relationships = []
    retrieved = []
    graded = []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(
        grade_sample, "get_cached_relationships", lambda: relationships
    )
    monkeypatch.setattr(grade_sample, "_matched_business_rules", lambda _q: [])
    monkeypatch.setattr(
        grade_sample,
        "_expand_schema_for_relationships",
        lambda full, tables, edges, _question: (full, edges),
    )
    monkeypatch.setattr(
        grade_sample,
        "_add_configured_search_context",
        lambda filtered, _full, edges, _all: (filtered, edges),
    )
    monkeypatch.setattr(
        grade_sample, "_flatten_schema_for_prompt", lambda _schema: "schema"
    )

    async def retrieve(_question, top_k=6):
        retrieved.append(top_k)
        return ["offer", "shop_details", "reviews"]

    async def grade(*args, **kwargs):
        graded.append(args[0])
        return True, []

    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)

    result = asyncio.run(grade_sample.grade_schema_sufficiency(question))

    assert result["sufficient"] is True
    assert retrieved == [6]
    assert graded == [question]


def test_shop_rating_policy_is_catalogued_not_encoded_as_a_fixed_query():
    rules = find_relevant_rules("Which is the best-rated shop?")

    rating_rule = next(
        rule for rule in rules if rule["term"] == "shop_rating_ranking"
    )
    assert "average of non-NULL reviews.rating" in rating_rule["definition"]
    assert "minimum review count" in rating_rule["definition"]


def test_ranked_result_note_uses_only_validated_plan_shape():
    metadata = ranked_result_metadata({
        "limit": 5,
        "order_by": [{"column": "avg_rating", "direction": "DESC"}],
        "select_items": [{
            "alias": "avg_rating",
            "metric": "average review rating",
        }],
    })

    assert metadata == {
        "metric": "average review rating",
        "column": "avg_rating",
        "direction": "DESC",
        "limit": 5,
    }
    note = build_ranked_result_note(metadata)
    assert "average review rating" in note
    assert "limit: 5" in note
