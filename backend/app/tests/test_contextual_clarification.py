import asyncio

import pytest

from app.agent.nodes import classifier_rewrite
from app.agent.nodes.classifier_rewrite import (
    _verified_followup_context_note,
    _normalize_llm_output,
    classify_and_rewrite,
)
from app.agent.graph import _inherit_verified_followup_filters
from app.agent.nodes.generate_query import QueryGenerationError


def _install_empty_catalogs(monkeypatch):
    monkeypatch.setattr(
        classifier_rewrite, "get_business_value_catalog", lambda: []
    )
    monkeypatch.setattr(
        classifier_rewrite, "get_business_rule_catalog", lambda: []
    )


def test_context_dependent_request_is_classified_by_structured_output(monkeypatch):
    class FakeLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return {
                "intent": "clarification",
                "complexity": "simple",
                "rewritten_question": "Which shops contributed to that?",
                "clarification_question": "Which result are you referring to?",
                "clarification_options": [],
                "clarification_topic": "entity",
            }

    monkeypatch.setattr(classifier_rewrite, "get_llm_service", lambda: FakeLLM())
    _install_empty_catalogs(monkeypatch)
    result = asyncio.run(
        classify_and_rewrite("Which shops contributed to that?")
    )
    assert result["intent"] == "clarification"
    assert result["clarification_question"]
    assert result["clarification_options"] == []
    assert result["clarification_topic"] == "entity"


def test_self_contained_percentage_question_reaches_structured_classifier(
    monkeypatch,
):
    question = (
        "Give me a short summary of our review ratings, including the average "
        "rating, total number of reviews, and percentage that are five-star."
    )
    calls = []
    class FakeLLM:
        async def call_agent_json(self, _agent_name, messages, **_kwargs):
            calls.append(messages)
            return {
                "intent": "data",
                "complexity": "complex",
                "rewritten_question": question,
                "clarification_question": "",
                "clarification_options": [],
            }

    monkeypatch.setattr(classifier_rewrite, "get_llm_service", lambda: FakeLLM())
    _install_empty_catalogs(monkeypatch)
    result = asyncio.run(classify_and_rewrite(question))
    assert calls
    assert result["intent"] == "data"
    assert result["clarification_question"] == ""


@pytest.mark.parametrize(
    "question",
    [
        "Which users have booked the exact same offer more than once?",
        "Show shops that sell snacks.",
        "Which shops have that service included in their offer?",
    ],
)
def test_complete_questions_with_referential_words_reach_classifier(
    monkeypatch, question
):
    calls = []

    class FakeLLM:
        async def call_agent_json(self, _agent_name, messages, **_kwargs):
            calls.append(messages)
            return {
                "intent": "data",
                "complexity": "simple",
                "rewritten_question": question,
                "clarification_question": "",
                "clarification_options": [],
            }

    monkeypatch.setattr(classifier_rewrite, "get_llm_service", lambda: FakeLLM())
    _install_empty_catalogs(monkeypatch)
    result = asyncio.run(classify_and_rewrite(question))

    assert calls
    assert result["intent"] == "data"


def test_classifier_receives_prior_context_for_followup_resolution(monkeypatch):
    messages_seen = []
    context = [{
        "columns": ["shop_name", "revenue"],
        "rows": [{"shop_name": "A1", "revenue": 100}],
    }]

    class FakeLLM:
        async def call_agent_json(self, _agent_name, messages, **_kwargs):
            messages_seen.extend(messages)
            return {
                "intent": "data",
                "complexity": "simple",
                "rewritten_question": "Which shops contributed to that?",
                "uses_prior_context": True,
                "clarification_question": "",
                "clarification_options": [],
            }

    monkeypatch.setattr(classifier_rewrite, "get_llm_service", lambda: FakeLLM())
    _install_empty_catalogs(monkeypatch)
    asyncio.run(
        classify_and_rewrite(
            "Which shops contributed to that?",
            prior_context=context,
        )
    )

    assert '"prior_verified_results"' in messages_seen[-1]["content"]


def test_clarification_options_must_be_present_in_verified_context():
    context = [{
        "columns": ["category", "booking_count"],
        "rows": [{"category": "Services", "booking_count": 8}],
        "metrics": [{"aggregate": "COUNT", "alias": "booking_count"}],
    }]
    result = _normalize_llm_output(
        {
            "intent": "clarification",
            "clarification_question": "Which result do you mean?",
            "clarification_options": ["Services", "invented result"],
        },
        "Which shops contributed to that?",
        context,
    )
    assert result["clarification_options"] == ["Services"]


def test_booking_revenue_category_uses_shop_category_by_default(monkeypatch):
    class FakeLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return {
                "intent": "clarification",
                "clarification_question": "Which category do you mean?",
                "clarification_options": [
                    "plan category",
                    "shop category",
                    "booking category",
                ],
            }

    monkeypatch.setattr(
        "app.agent.nodes.classifier_rewrite.get_llm_service",
        lambda: FakeLLM(),
    )
    _install_empty_catalogs(monkeypatch)
    result = asyncio.run(
        classify_and_rewrite(
            "Which category generated the most completed booking revenue?"
        )
    )

    assert result["intent"] == "data"
    assert result["clarification_question"] == ""


def test_generic_reference_to_multiple_metrics_is_delegated_to_classifier(monkeypatch):
    context = [{
        "metrics": [
            {"aggregate": "COUNT", "alias": "booking_count"},
            {"aggregate": "SUM", "alias": "total_revenue"},
        ],
    }]

    class FakeLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return {
                "intent": "data",
                "complexity": "simple",
                "rewritten_question": "Which shops contributed most to that?",
                "clarification_question": "",
                "clarification_options": [],
            }

    monkeypatch.setattr(classifier_rewrite, "get_llm_service", lambda: FakeLLM())
    _install_empty_catalogs(monkeypatch)
    result = asyncio.run(
        classify_and_rewrite(
            "Which shops contributed most to that?",
            prior_context=context,
        )
    )
    assert result["intent"] == "data"
    assert result["clarification_question"] == ""


def test_followup_context_note_retains_metric_source_and_prior_filters():
    context = [{
        "query": {"table": "bookings"},
        "metrics": [{
            "table": "bookings",
            "column": "final_amount",
            "aggregate": "SUM",
            "alias": "total_revenue",
        }],
        "filters": {
            "boolean": "AND",
            "conditions": [
                {"table": "bookings", "column": "status", "operator": "=", "value": 1},
                {
                    "table": "bookings",
                    "column": "time_slot_date",
                    "operator": ">=",
                    "value": "2025-01-01",
                },
            ],
        },
    }]

    note = _verified_followup_context_note(context)
    assert "source table bookings" in note
    assert "SUM(bookings.final_amount) as total_revenue" in note
    assert "bookings.status = 1" in note
    assert "2025-01-01" in note


def test_followup_plan_inherits_prior_filters_without_dropping_current_filter():
    prior_turn = {
        "filters": {
            "boolean": "AND",
            "conditions": [
                {"table": "bookings", "column": "status", "operator": "=", "value": 1},
                {
                    "table": "bookings",
                    "column": "time_slot_date",
                    "operator": ">=",
                    "value": "2025-01-01",
                },
            ],
        },
    }
    plan = {
        "table": "bookings",
        "joins": [],
        "where_expression": {
            "table": "bookings",
            "column": "time_slot_month",
            "operator": "=",
            "value": "May",
        },
    }

    updated = _inherit_verified_followup_filters(plan, prior_turn)
    assert updated["where_expression"]["boolean"] == "AND"
    assert updated["where_expression"]["conditions"][0] == plan["where_expression"]
    assert updated["where_expression"]["conditions"][1] == prior_turn["filters"]


def test_followup_filter_inheritance_fails_closed_if_source_table_is_missing():
    with pytest.raises(QueryGenerationError, match="did not include the table"):
        _inherit_verified_followup_filters(
            {"table": "coupon_purchase_payment", "joins": [], "where_expression": None},
            {
                "filters": {
                    "table": "bookings",
                    "column": "status",
                    "operator": "=",
                    "value": 1,
                },
            },
        )
