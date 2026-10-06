"""
classifier_rewrite.py  (Node 1: Query Classifier + Rewrite + Casual chat)

Entry point of the graph. For each raw question it decides:
  - intent "data": classify simple/complex and rewrite precisely.
  - intent "chat": casual talk. Returns a friendly reply and the graph skips
    the whole database pipeline.

Uses the cheap/fast model: this runs on every single question.
"""

import logging
import re
from typing import Any, Dict

from app.agent.prompts.classifier_prompt import build_classifier_messages
from app.services.llm_service import LLMServiceError, get_llm_service

logger = logging.getLogger(__name__)

VALID_COMPLEXITY_VALUES = {"simple", "complex"}
VALID_INTENTS = {"data", "chat"}

MAX_QUESTION_LENGTH = 2000
MAX_CHAT_REPLY_LENGTH = 300

DEFAULT_CHAT_REPLY = (
    "Hi! I can answer questions about your business data. "
    "Try asking something like \"How many plans do we have?\""
)


# Words the rewrite must not add unless the user already used them.
_ADDED_COLUMN_WORDS = ("phone", "website", "email", "address", "pincode")
# If the user used any of these, extra detail columns are plausible.
_DETAIL_HINTS = ("contact", "detail", "number", "mobile", "url", "site", "link")
# "named" is fine after these words (category names are legitimate).
_NAMED_OK_AFTER = {"category", "subcategory"}


def _find_rewrite_drift(original: str, rewritten: str) -> str | None:
    """
    Returns a short description of what the rewrite changed wrongly, or None.

    Catches two known classifier mistakes:
      1. Columns the user never asked for (phone, website, ...).
      2. "from X" / "in X" turned into "named X".
    """
    orig = original.lower()
    new = rewritten.lower()

    # 1. Added columns
    if not any(hint in orig for hint in _DETAIL_HINTS):
        for word in _ADDED_COLUMN_WORDS:
            if word in new and word not in orig:
                return f"added column {word!r}"

    # 2. Invented "named X"
    if not re.search(r"\b(named|called|name)\b", orig):
        for match in re.finditer(r"(\w+)\s+(?:named|called)\b", new):
            if match.group(1) not in _NAMED_OK_AFTER:
                return f"added 'named' after {match.group(1)!r}"

    return None


class ClassificationError(Exception):
    """
    Raised when the question can't be classified: bad input, or an invalid
    LLM response. The graph routes this to Node 11 (escalation).
    """
    pass


def _validate_input(user_question: str) -> str:
    """Validates and normalizes the raw question before it goes anywhere."""
    if user_question is None:
        raise ClassificationError("user_question is required and was None.")

    cleaned = user_question.strip()

    if not cleaned:
        raise ClassificationError("user_question is empty after stripping whitespace.")

    if len(cleaned) > MAX_QUESTION_LENGTH:
        raise ClassificationError(
            f"user_question exceeds max length of {MAX_QUESTION_LENGTH} characters "
            f"(got {len(cleaned)})."
        )

    return cleaned


def _normalize_llm_output(parsed: Dict[str, Any], cleaned_question: str) -> Dict[str, Any]:
    """
    Checks the LLM's JSON and returns a clean, complete dict.

    Lenient where a glitch is harmless (missing reply -> default reply,
    data intent with an empty rewrite -> treat as casual chat) and strict
    where guessing would be unsafe (unknown intent or complexity).
    """
    if not isinstance(parsed, dict):
        raise ClassificationError(f"LLM response is not a JSON object: {parsed!r}")

    # Older responses had no "intent": treat them as data questions.
    intent = str(parsed.get("intent", "data")).strip().lower()
    if intent not in VALID_INTENTS:
        raise ClassificationError(
            f"LLM returned invalid intent: {intent!r}. Expected one of {VALID_INTENTS}."
        )

    reasoning = str(parsed.get("reasoning", "")).strip()
    rewritten = parsed.get("rewritten_question")
    complexity = "simple"

    if intent == "data":
        if not isinstance(rewritten, str) or not rewritten.strip():
            # Nothing to rewrite: the model glitched or it is really small talk.
            logger.warning("Node 1: data intent with empty rewrite for %r; treating as chat.", cleaned_question)
            intent = "chat"
            parsed["chat_reply"] = None
        else:
            complexity = parsed.get("complexity")
            if complexity not in VALID_COMPLEXITY_VALUES:
                raise ClassificationError(
                    f"LLM returned invalid complexity value: {complexity!r}. "
                    f"Expected one of {VALID_COMPLEXITY_VALUES}."
                )

    if intent == "chat":
        reply = parsed.get("chat_reply")
        if not isinstance(reply, str) or not reply.strip():
            reply = DEFAULT_CHAT_REPLY
        return {
            "intent": "chat",
            "complexity": "simple",
            "rewritten_question": cleaned_question,
            "chat_reply": reply.strip()[:MAX_CHAT_REPLY_LENGTH],
            "reasoning": reasoning,
        }

    return {
        "intent": "data",
        "complexity": complexity,
        "rewritten_question": rewritten.strip(),
        "chat_reply": None,
        "reasoning": reasoning,
    }


async def classify_and_rewrite(user_question: str) -> Dict[str, Any]:
    """
    Main entry point for Node 1.

    Returns:
        {
            "original_question": str,
            "rewritten_question": str,   # same as original for chat
            "intent": "data" | "chat",
            "complexity": "simple" | "complex",
            "chat_reply": str | None,    # only set when intent == "chat"
            "reasoning": str,
        }

    Raises ClassificationError on bad input or an unusable LLM response.
    """
    cleaned_question = _validate_input(user_question)

    llm = get_llm_service()
    messages = build_classifier_messages(cleaned_question)

    normalized = None
    for attempt in (1, 2):
        try:
            parsed = await llm.call_agent_json("CLASSIFIER", messages, temperature=0.0)
        except LLMServiceError as e:
            raise ClassificationError(f"LLM call failed during classification: {e}") from e

        normalized = _normalize_llm_output(parsed, cleaned_question)
        if normalized["intent"] != "data":
            break

        drift = _find_rewrite_drift(cleaned_question, normalized["rewritten_question"])
        if drift is None:
            break

        logger.warning(
            "Node 1: rewrite drift (%s) on attempt %d: %r -> %r",
            drift, attempt, cleaned_question, normalized["rewritten_question"],
        )
        if attempt == 1:
            # Retry once, telling the model exactly what it did wrong.
            messages = build_classifier_messages(cleaned_question) + [
                {"role": "assistant", "content": str(parsed)},
                {
                    "role": "user",
                    "content": (
                        f"Your rewrite was wrong: {drift}. Rewrite again. Do not add "
                        "columns and do not change the user's wording. Return the "
                        "same JSON shape."
                    ),
                },
            ]
        else:
            # Still wrong: the original question is safer than a bad rewrite.
            normalized["rewritten_question"] = cleaned_question
            logger.warning("Node 1: using the original question as the rewrite.")

    result = {"original_question": cleaned_question, **normalized}

    logger.info(
        "Classified as intent=%s complexity=%s: %r -> %r",
        result["intent"], result["complexity"],
        result["original_question"], result["rewritten_question"],
    )
    return result