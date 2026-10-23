"""
test_classifier_rewrite.py — manual smoke test for classifier_rewrite.py (Node 1)

Calls the real Groq API — costs a small amount of tokens to run.

Run from backend/:
    python -m app.tests.test_classifier_rewrite
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import pytest

load_dotenv()

from app.agent import catalog_resolver
from app.agent.nodes import classifier_rewrite
from app.services.date_resolver import resolve_temporal_intents
from app.agent.nodes.classifier_rewrite import (
    ClassificationError,
    classify_and_rewrite,
)

TEST_QUESTIONS = [
    "Show me high value accounts",
    "How many customers bought stock and then sold it within the same month, grouped by tier",
    "give me the ones in asia that are active",  # deliberately vague, to test rewriting
]

NON_FILTER_SHOP_QUESTIONS = [
    "List viewed offers",
    "How many free services do we offer?",
    "Show bookings grouped by cities",
    "Hi, could you tell me howmany active shops we have?",
    "Hi, could you tell me how many active shops we have?",
    "Summarize how active shops are distributed across categories.",
    "Hi, how many shops are there?",
    "Hello, show me active shops",
    "Could you tell me how many active shops we have?",
    "Please list active shops",
    "Summarize shops by category",
    "Give me a summary of shops per category",
]


@pytest.mark.parametrize("question", NON_FILTER_SHOP_QUESTIONS)
def test_non_filter_shop_questions_bypass_lexical_shop_type_resolution(
    monkeypatch,
    question,
):
    async def classify(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        catalog_resolver,
        "resolve_business_request",
        lambda *_args, **_kwargs: pytest.fail(
            "Unstructured business-type resolver must not run."
        ),
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "get_business_value_catalog",
        lambda: [],
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "get_business_rule_catalog",
        lambda: [],
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "get_llm_service",
        lambda: SimpleNamespace(call_agent_json=classify),
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "_normalize_llm_output",
        lambda *_args: {
            "intent": "data",
            "complexity": "simple",
            "rewritten_question": question,
            "chat_reply": "",
            "reasoning": "A complete database question.",
            "clarification_question": "",
            "clarification_options": [],
        },
    )
    monkeypatch.setenv("USE_QUESTION_REWRITE", "false")

    result = asyncio.run(classify_and_rewrite(question))

    assert result["intent"] == "data"


def test_classifier_resolves_structured_dates_and_catalog_state_filters(monkeypatch):
    state_rule = {
        "term": "active_shop_status",
        "definition": "Active shops have shop_details.status = 1.",
        "query_logic": {
            "state_filter": {
                "entity": "shop_details",
                "state": "active",
                "table": "shop_details",
                "column": "status",
                "value": 1,
            },
        },
    }

    async def classify(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(classifier_rewrite, "get_business_value_catalog", lambda: [])
    monkeypatch.setattr(
        classifier_rewrite,
        "get_business_rule_catalog",
        lambda: [{
            "term": state_rule["term"],
            "definition": state_rule["definition"],
            "state_filter": state_rule["query_logic"]["state_filter"],
        }],
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "find_rules_by_terms",
        lambda terms: [state_rule] if state_rule["term"] in terms else [],
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "get_llm_service",
        lambda: SimpleNamespace(call_agent_json=classify),
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "_normalize_llm_output",
        lambda *_args: {
            "intent": "data",
            "complexity": "simple",
            "rewritten_question": "How many bookings last month and currently active shops?",
            "chat_reply": "",
            "reasoning": "The question has a relative period and active-shop state.",
            "clarification_question": "",
            "clarification_options": [],
            "temporal_intents": [
                {
                    "kind": "relative_period",
                    "target": "bookings",
                    "window": "previous_complete",
                    "unit": "month",
                    "count": 1,
                },
                {
                    "kind": "current_state",
                    "entity": "shop_details",
                    "state": "active",
                    "business_rule_term": "active_shop_status",
                },
            ],
            "shop_type_entities": [],
            "matched_business_rule_terms": ["active_shop_status"],
            "catalog_filter_combiner": None,
        },
    )
    real_resolver = resolve_temporal_intents
    monkeypatch.setattr(
        classifier_rewrite,
        "resolve_temporal_intents",
        lambda intents: real_resolver(
            intents,
            now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
        ),
    )
    monkeypatch.setenv("USE_QUESTION_REWRITE", "false")

    result = asyncio.run(
        classify_and_rewrite("How many bookings last month and currently active shops?")
    )

    assert result["resolved_temporal_constraints"][0]["start"] == "2026-09-01"
    assert result["resolved_temporal_constraints"][0]["end"] == "2026-10-01"
    assert result["current_state_filters"] == [{
        "kind": "current_state",
        **state_rule["query_logic"]["state_filter"],
        "business_rule_term": "active_shop_status",
    }]


def test_classifier_retries_once_after_invalid_structured_temporal_output(monkeypatch):
    valid_response = {
        "intent": "data",
        "complexity": "simple",
        "rewritten_question": "How many bookings did we get last month?",
        "chat_reply": "",
        "reasoning": "A booking count for the previous month.",
        "clarification_question": "",
        "clarification_options": [],
        "temporal_intents": [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "previous_complete",
            "unit": "month",
            "count": 1,
        }],
        "shop_type_entities": [],
        "matched_business_rule_terms": [],
        "catalog_filter_combiner": "AND",
    }
    invalid_response = {
        **valid_response,
        "temporal_intents": [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "previous_complete",
            "unit": "month",
        }],
    }
    responses = [invalid_response, valid_response]
    calls = []

    async def classify(_role, messages, **_kwargs):
        calls.append(messages)
        return responses.pop(0)

    monkeypatch.setattr(classifier_rewrite, "get_business_value_catalog", lambda: [])
    monkeypatch.setattr(classifier_rewrite, "get_business_rule_catalog", lambda: [])
    monkeypatch.setattr(
        classifier_rewrite,
        "get_llm_service",
        lambda: SimpleNamespace(call_agent_json=classify),
    )
    monkeypatch.setenv("USE_QUESTION_REWRITE", "false")

    result = asyncio.run(
        classify_and_rewrite("How many bookings did we get last month?")
    )

    assert len(calls) == 2
    assert "count" in calls[1][-1]["content"]
    assert result["resolved_temporal_constraints"][0]["start"] == "2026-09-01"
    assert result["resolved_temporal_constraints"][0]["end"] == "2026-10-01"


def test_quoted_shop_name_does_not_trigger_entity_clarification(monkeypatch):
    async def classify(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(classifier_rewrite, "get_business_value_catalog", lambda: [])
    monkeypatch.setattr(classifier_rewrite, "get_business_rule_catalog", lambda: [])
    monkeypatch.setattr(classifier_rewrite, "find_rules_by_terms", lambda _terms: [])
    monkeypatch.setattr(classifier_rewrite, "shop_name_matches_term", lambda term: term == "Timepass")
    monkeypatch.setattr(
        classifier_rewrite,
        "get_llm_service",
        lambda: SimpleNamespace(call_agent_json=classify),
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "_normalize_llm_output",
        lambda *_args: {
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": "Show the bookings at 'Timepass'.",
            "chat_reply": "",
            "reasoning": "The shop name is explicit.",
            "clarification_question": "Which shop named Timepass?",
            "clarification_options": [],
            "clarification_topic": "entity",
            "temporal_intents": [],
            "shop_type_entities": [],
            "matched_business_rule_terms": [],
            "catalog_filter_combiner": None,
        },
    )
    monkeypatch.setenv("USE_QUESTION_REWRITE", "false")

    result = asyncio.run(classify_and_rewrite("Show the bookings at 'Timepass'."))

    assert result["intent"] == "data"
    assert result["clarification_question"] == ""


async def main() -> None:
    print("1. Testing normal questions...\n")
    for question in TEST_QUESTIONS:
        print(f"Original:  {question!r}")
        try:
            result = await classify_and_rewrite(question)
            print(f"Rewritten: {result['rewritten_question']!r}")
            print(f"Complexity: {result['complexity']}")
            print(f"Reasoning:  {result['reasoning']}\n")
        except ClassificationError as e:
            print(f"   FAILED: {e}\n")

    print("2. Testing edge cases...\n")

    print("Empty string:")
    try:
        await classify_and_rewrite("")
        print("   FAILED: expected ClassificationError but none was raised.\n")
    except ClassificationError as e:
        print(f"   Correctly raised ClassificationError: {e}\n")

    print("Whitespace-only string:")
    try:
        await classify_and_rewrite("     ")
        print("   FAILED: expected ClassificationError but none was raised.\n")
    except ClassificationError as e:
        print(f"   Correctly raised ClassificationError: {e}\n")

    print("Extremely long string (over 2000 chars):")
    try:
        await classify_and_rewrite("a" * 3000)
        print("   FAILED: expected ClassificationError but none was raised.\n")
    except ClassificationError as e:
        print(f"   Correctly raised ClassificationError: {e}\n")

    print("All checks completed.")


if __name__ == "__main__":
    asyncio.run(main())