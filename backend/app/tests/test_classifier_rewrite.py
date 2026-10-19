"""
test_classifier_rewrite.py — manual smoke test for classifier_rewrite.py (Node 1)

Calls the real Groq API — costs a small amount of tokens to run.

Run from backend/:
    python -m app.tests.test_classifier_rewrite
"""

import asyncio
from types import SimpleNamespace

from dotenv import load_dotenv
import pytest

load_dotenv()

from app.agent import catalog_resolver
from app.agent.nodes import classifier_rewrite
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
def test_non_filter_shop_questions_reach_classifier_without_catalog_lookup(
    monkeypatch,
    question,
):
    resolver_results = []
    original_resolver = catalog_resolver.resolve_business_request

    def observe_resolver(*args, **kwargs):
        result = original_resolver(*args, **kwargs)
        resolver_results.append(result)
        return result

    async def classify(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        classifier_rewrite, "resolve_business_request", observe_resolver
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

    assert resolver_results == [None]
    assert result["intent"] == "data"


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