import asyncio

import pytest

from app.agent.nodes import grade_sample


def test_schema_grader_uses_original_question_not_invented_field(monkeypatch):
    original = "who sells fish meals?"
    rewrite = "Show shops whose name or description includes 'fish meals'"
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "name", "data_type": "varchar", "key": ""},
        ]
    }
    retrieved_questions = []
    grader_inputs = []

    async def retrieve(question, top_k=6):
        retrieved_questions.append(question)
        return ["shop_details"]

    async def grade(original_question, rewritten_question, schema_text, relationships):
        grader_inputs.append(
            (original_question, rewritten_question, schema_text)
        )
        assert "description" not in schema_text
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(
        grade_sample,
        "_expand_schema_for_relationships",
        lambda full_schema, tables, relationships, question: (schema, []),
    )
    monkeypatch.setattr(
        grade_sample,
        "_flatten_schema_for_prompt",
        lambda selected: "shop_details: id, name",
    )
    monkeypatch.setattr(grade_sample, "_matched_business_rules", lambda question: [])
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)

    result = asyncio.run(
        grade_sample.grade_schema_sufficiency(rewrite, original)
    )

    assert result["sufficient"] is True
    assert retrieved_questions == [original]
    assert grader_inputs == [
        (original, rewrite, "shop_details: id, name")
    ]


def test_follow_up_retrieval_uses_only_original_question(monkeypatch):
    original = "What about those in Nagercoil?"
    rewrite = "Show shops in Nagercoil"
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "name", "data_type": "varchar", "key": ""},
        ]
    }
    retrieved_questions = []
    expansion_questions = []
    grader_inputs = []

    async def retrieve(question, top_k=6):
        retrieved_questions.append(question)
        return ["shop_details"]

    def expand(full_schema, tables, relationships, question):
        expansion_questions.append(question)
        return schema, []

    async def grade(original_question, rewritten_question, schema_text, relationships):
        grader_inputs.append((original_question, rewritten_question))
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_expand_schema_for_relationships", expand)
    monkeypatch.setattr(
        grade_sample, "_flatten_schema_for_prompt", lambda selected: "shop_details"
    )
    monkeypatch.setattr(grade_sample, "_matched_business_rules", lambda question: [])
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)

    result = asyncio.run(grade_sample.grade_schema_sufficiency(rewrite, original))

    assert result["sufficient"] is True
    assert retrieved_questions == [original]
    assert expansion_questions == [original]
    assert grader_inputs == [(original, rewrite)]


@pytest.mark.parametrize(
    ("question", "missing_detail", "expected_alias"),
    [
        ("What is the savings amount?", "missing: savings_amount", "savings amount"),
        ("What is the discount amount?", "discount amount column for offers", "discount amount"),
        (
            "Which offers have the largest price drop?",
            "largest price drop column for offers",
            "largest price drop",
        ),
    ],
)
def test_schema_grader_treats_matched_savings_metric_as_available(
    monkeypatch, question, missing_detail, expected_alias
):
    schema = {
        "offer": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "actual_price", "data_type": "decimal", "key": ""},
            {"column": "offer_price", "data_type": "decimal", "key": ""},
            {"column": "offer_price_p", "data_type": "decimal", "key": ""},
        ]
    }
    captured = {}

    async def retrieve(retrieval_question, top_k=6):
        return ["offer"]

    async def grade(
        original_question,
        rewritten_question,
        schema_text,
        relationships,
        available_calculated_metrics=None,
    ):
        captured["metrics"] = available_calculated_metrics
        prompt = grade_sample._build_schema_grader_prompt(
            original_question,
            rewritten_question,
            schema_text,
            relationships,
            available_calculated_metrics=available_calculated_metrics,
        )
        assert "savings (also called:" in prompt
        assert "code-defined values, not physical schema columns" in prompt
        return False, [{
            "type": "column",
            "detail": missing_detail,
            "tables": ["offer"],
        }]

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(
        grade_sample,
        "_expand_schema_for_relationships",
        lambda full, tables, relationships, original: (schema, []),
    )
    monkeypatch.setattr(
        grade_sample, "_flatten_schema_for_prompt", lambda selected: "offer: id, actual_price"
    )
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)

    result = asyncio.run(
        grade_sample.grade_schema_sufficiency(question, question)
    )

    assert result["sufficient"] is True
    assert result["attempts"] == 1
    assert "savings" in captured["metrics"]
    assert expected_alias in captured["metrics"]["savings"]


def test_schema_grader_keeps_real_missing_column_alongside_metric_name():
    metric_context = {
        "savings": ["savings", "savings amount", "discount amount"]
    }
    missing = {
        "type": "column",
        "detail": "missing discount amount and status column",
        "tables": ["offer"],
    }

    assert grade_sample._metric_for_missing_column(missing, metric_context) is None
