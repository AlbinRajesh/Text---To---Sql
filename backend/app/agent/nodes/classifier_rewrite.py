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
import os
from typing import Any, Dict

from app.agent.nodes.schema_grounding import SchemaGroundingError, get_cached_schema
from app.agent.prompts.classifier_prompt import build_classifier_messages
from app.services.llm_service import (
    LLMRefusalError,
    LLMServiceError,
    get_llm_service,
)

logger = logging.getLogger(__name__)

VALID_COMPLEXITY_VALUES = {"simple", "complex"}
VALID_INTENTS = {"data", "chat"}

MAX_QUESTION_LENGTH = 2000
MAX_CHAT_REPLY_LENGTH = 300

REFUSAL_REPLY = (
    "I can't help with that request. "
    "I can answer questions about your business data."
)

DEFAULT_CHAT_REPLY = (
    "Hi! I can answer questions about your business data. "
    "Try asking something like \"How many plans do we have?\""
)

_NAMED_OK_AFTER = {"category", "subcategory"}
_SCHEMA_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_SCHEMA_WORD_STOP_LIST = {
    "and",
    "are",
    "for",
    "from",
    "the",
    "was",
    "were",
    "with",
}


def _schema_terms(schema_context: Dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for table, columns in schema_context.items():
        for identifier in [table, *(
            column.get("column")
            for column in columns
            if isinstance(column, dict) and isinstance(column.get("column"), str)
        )]:
            terms.update(
                term
                for term in _SCHEMA_WORD_PATTERN.findall(identifier.casefold())
                if len(term) >= 3 and term not in _SCHEMA_WORD_STOP_LIST
            )
    return terms


def _find_rewrite_drift(
    original: str,
    rewritten: str,
    schema_context: Dict[str, Any],
) -> str | None:
    """Return a detected location/value or schema term added by the rewrite."""
    if not re.search(r"\b(named|called|name)\b", original, re.IGNORECASE):
        for match in re.finditer(r"(\w+)\s+(?:named|called)\b", rewritten, re.IGNORECASE):
            if match.group(1).casefold() not in _NAMED_OK_AFTER:
                return f"changed a location/value into 'named' after {match.group(1)!r}"

    original_words = set(_SCHEMA_WORD_PATTERN.findall(original.casefold()))
    rewritten_words = set(_SCHEMA_WORD_PATTERN.findall(rewritten.casefold()))
    added_schema_words = (
        rewritten_words - original_words
    ) & _schema_terms(schema_context)
    if added_schema_words:
        return f"added schema field or table word {sorted(added_schema_words)[0]!r}"
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
            "rewritten_question": str,   # same as original for chat, and for
                                         # data when USE_QUESTION_REWRITE is off
            "intent": "data" | "chat",
            "complexity": "simple" | "complex",
            "chat_reply": str | None,    # only set when intent == "chat"
            "reasoning": str,
        }

    Raises ClassificationError on bad input or an unusable LLM response.
    """
    cleaned_question = _validate_input(user_question)

    # When off, the LLM is still used for intent and complexity, but its
    # rewrite is ignored so invented database values never reach later nodes.
    use_rewrite = os.getenv("USE_QUESTION_REWRITE", "true").strip().lower() in {
        "1", "true", "yes",
    }

    llm = get_llm_service()
    messages = build_classifier_messages(cleaned_question)

    try:
        parsed = await llm.call_agent_json("CLASSIFIER", messages, temperature=0.0)
    except LLMRefusalError:
        # The model declined (e.g. a prompt-injection attempt). Stop here
        # with a fixed safe reply. The model's own text is never shown.
        logger.warning("Node 1: model declined %r; sending safe refusal.", cleaned_question)
        return {
            "original_question": cleaned_question,
            "intent": "chat",
            "complexity": "simple",
            "rewritten_question": cleaned_question,
            "chat_reply": REFUSAL_REPLY,
            "reasoning": "The model declined this request.",
        }
    except LLMServiceError as e:
        raise ClassificationError(f"LLM call failed during classification: {e}") from e

    normalized = _normalize_llm_output(parsed, cleaned_question)
    if normalized["intent"] == "data":
        if not use_rewrite:
            normalized["rewritten_question"] = cleaned_question
        else:
            try:
                schema_context = get_cached_schema()
            except SchemaGroundingError:
                normalized["rewritten_question"] = cleaned_question
                logger.warning(
                    "Node 1: schema cache unavailable; using the original wording."
                )
            else:
                drift = _find_rewrite_drift(
                    cleaned_question,
                    normalized["rewritten_question"],
                    schema_context,
                )
                if drift is not None:
                    logger.warning(
                        "Node 1: rewrite drift (%s): %r -> %r; using original wording.",
                        drift,
                        cleaned_question,
                        normalized["rewritten_question"],
                    )
                    normalized["rewritten_question"] = cleaned_question

    result = {"original_question": cleaned_question, **normalized}

    logger.info(
        "Classified as intent=%s complexity=%s rewrite_enabled=%s: %r -> %r",
        result["intent"], result["complexity"], use_rewrite,
        result["original_question"], result["rewritten_question"],
    )
    return result