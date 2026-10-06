"""
test_classifier_rewrite.py — manual smoke test for classifier_rewrite.py (Node 1)

Calls the real Groq API — costs a small amount of tokens to run.

Run from backend/:
    python -m app.tests.test_classifier_rewrite
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.agent.nodes.classifier_rewrite import classify_and_rewrite, ClassificationError

TEST_QUESTIONS = [
    "Show me high value accounts",
    "How many customers bought stock and then sold it within the same month, grouped by tier",
    "give me the ones in asia that are active",  # deliberately vague, to test rewriting
]


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