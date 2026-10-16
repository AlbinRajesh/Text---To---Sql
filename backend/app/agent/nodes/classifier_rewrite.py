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
import json
from pathlib import Path
from typing import Any, Dict, Sequence

from app.agent.nodes.schema_grounding import SchemaGroundingError, get_cached_schema
from app.agent.prompts.classifier_prompt import (
    bound_classifier_context,
    build_classifier_messages,
)
from app.services.llm_service import (
    LLMRefusalError,
    LLMServiceError,
    get_llm_service,
)
from app.agent.catalog_resolver import resolve_business_request
from app.services.clarification_gate import is_askable
from app.services.value_index import (
    ValueIndexError,
    get_business_value_catalog,
)

logger = logging.getLogger(__name__)

VALID_COMPLEXITY_VALUES = {"simple", "complex"}
VALID_INTENTS = {"data", "chat", "clarification"}
_CLARIFICATION_TOPIC_CONFIG = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "rules"
        / "clarification_topics.json"
    ).read_text(encoding="utf-8")
)
if (
    not isinstance(_CLARIFICATION_TOPIC_CONFIG, dict)
    or not isinstance(_CLARIFICATION_TOPIC_CONFIG.get("allowed_topics"), list)
    or any(
        not isinstance(topic, str)
        for topic in _CLARIFICATION_TOPIC_CONFIG["allowed_topics"]
    )
):
    raise ValueError("Invalid allowed clarification topic configuration.")
ALLOWED_CLARIFICATION_TOPICS = frozenset(
    _CLARIFICATION_TOPIC_CONFIG["allowed_topics"]
)
_CLARIFICATION_STOP_WORDS = frozenset(
    json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "rules"
            / "clarification_words.json"
        ).read_text(encoding="utf-8")
    )["en"]["stop_words"]
)

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
_REFERENTIAL_QUESTION_PATTERN = re.compile(
    r"\b(?:that|those|these|them|it|same)\b"
    r"|\bwhat\s+about\s+the\s+(?:first|second|last|next|previous)\s+one\b",
    re.IGNORECASE,
)
_BOOKING_REVENUE_CATEGORY_PATTERN = re.compile(
    r"\bcategory\b.*\bbooking\b.*\brevenue\b"
    r"|\bbooking\b.*\brevenue\b.*\bcategory\b",
    re.IGNORECASE,
)
_SELF_CONTAINED_DATA_REQUEST_PATTERN = re.compile(
    r"^\s*(?:what\s+percentage\b|which\s+subcategories?\b)"
    r".*\b(?:offers?|discount|subcategories?|shops?|active|most|greatest|highest)\b",
    re.IGNORECASE,
)
_SHOP_STATUS_COMPARISON_PATTERN = re.compile(
    r"\b(?:for\s+each|per|by)\s+(?:the\s+)?categor(?:y|ies)\b"
    r".*\bactive\b.*\binactive\b"
    r"|\bactive\b.*\binactive\b"
    r".*\b(?:for\s+each|per|by)\s+(?:the\s+)?categor(?:y|ies)\b",
    re.IGNORECASE,
)
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


def _ambiguous_metric_options(
    question: str,
    prior_context: list[Dict[str, Any]],
) -> list[str]:
    """Return multiple verified metric aliases when a generic reference is ambiguous."""
    if not _REFERENTIAL_QUESTION_PATTERN.search(question) or not prior_context:
        return []
    latest = prior_context[-1]
    if not isinstance(latest, dict):
        return []
    aliases = list(dict.fromkeys(
        metric["alias"]
        for metric in latest.get("metrics", [])
        if isinstance(metric, dict)
        and metric.get("aggregate") is not None
        and isinstance(metric.get("alias"), str)
    ))[:4]
    if len(aliases) < 2:
        return []
    matching_aliases = [
        alias
        for alias in aliases
        if any(
            re.search(rf"\b{re.escape(part)}\b", question, re.IGNORECASE)
            for part in alias.split("_")
            if len(part) > 2
        )
    ]
    if len(matching_aliases) == 1:
        return []
    return aliases


def _has_context_reference(question: str) -> bool:
    for match in _REFERENTIAL_QUESTION_PATTERN.finditer(question):
        if match.group().casefold() == "that":
            following_word = re.match(r"\s+([^\W_]+)", question[match.end():])
            if (
                following_word
                and following_word.group(1).casefold()
                in _CLARIFICATION_STOP_WORDS
            ):
                continue
        return True
    return False


def _format_context_filter(expression: Any) -> str:
    if not isinstance(expression, dict):
        return ""
    boolean = expression.get("boolean")
    if boolean in {"AND", "OR"}:
        children = [
            _format_context_filter(child)
            for child in expression.get("conditions", [])
        ]
        return f" {boolean} ".join(child for child in children if child)
    table = expression.get("table")
    column = expression.get("column")
    operator = expression.get("operator")
    if not all(isinstance(value, str) and value for value in (table, column, operator)):
        return ""
    if operator in {"IS NULL", "IS NOT NULL"}:
        return f"{table}.{column} {operator}"
    try:
        value = json.dumps(expression.get("value"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""
    return f"{table}.{column} {operator} {value}"


def _verified_followup_context_note(prior_context: list[Dict[str, Any]]) -> str:
    if not prior_context:
        return ""
    latest = prior_context[-1]
    if not isinstance(latest, dict):
        return ""
    query = latest.get("query")
    parts = []
    if isinstance(query, dict) and isinstance(query.get("table"), str):
        parts.append(f"source table {query['table']}")
    metrics = []
    for metric in latest.get("metrics", []):
        if not isinstance(metric, dict):
            continue
        table = metric.get("table")
        column = metric.get("column")
        aggregate = metric.get("aggregate")
        alias = metric.get("alias")
        if isinstance(table, str) and isinstance(column, str) and isinstance(aggregate, str):
            label = f"{aggregate}({table}.{column})"
            if isinstance(alias, str):
                label += f" as {alias}"
            metrics.append(label)
    if metrics:
        parts.append("metric " + ", ".join(metrics[:4]))
    filter_note = _format_context_filter(latest.get("filters"))
    if filter_note:
        parts.append(f"filters {filter_note}")
    if not parts:
        return ""
    return (
        " Preserve the applicable verified context for this follow-up: "
        + "; ".join(parts)
        + ". Do not replace the prior metric with its numeric result."
    )


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


def _normalize_llm_output(
    parsed: Dict[str, Any],
    cleaned_question: str,
    prior_context: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
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
    if intent == "clarification" or parsed.get("clarification_needed") is True:
        clarification_question = parsed.get("clarification_question")
        if not isinstance(clarification_question, str) or not clarification_question.strip():
            raise ClassificationError(
                "A clarification response must contain a non-empty clarification_question."
            )
        options = parsed.get("clarification_options", [])
        if (
            not isinstance(options, list)
            or len(options) > 4
            or any(not isinstance(option, str) or not option.strip() for option in options)
        ):
            raise ClassificationError(
                "clarification_options must contain at most four non-empty strings."
            )
        known_values = set()
        for turn in prior_context or []:
            if not isinstance(turn, dict):
                continue
            known_values.update(
                value.strip().casefold().replace("_", " ")
                for value in turn.get("columns", [])
                if isinstance(value, str)
            )
            for row in turn.get("rows", []):
                if isinstance(row, dict):
                    known_values.update(
                        str(value).strip().casefold().replace("_", " ")
                        for value in row.values()
                        if isinstance(value, (str, int, float))
                    )
            for metric in turn.get("metrics", []):
                if isinstance(metric, dict):
                    known_values.update(
                        str(metric[key]).strip().casefold().replace("_", " ")
                        for key in ("alias", "column", "aggregate")
                        if isinstance(metric.get(key), (str, int, float))
                    )
        grounded_options = []
        for option in options:
            cleaned_option = option.strip()
            normalized_option = cleaned_option.casefold().replace("_", " ")
            if normalized_option in known_values and cleaned_option not in grounded_options:
                grounded_options.append(cleaned_option)
        if len(grounded_options) != len(options):
            logger.warning(
                "Node 1: removed clarification options not grounded in verified context."
            )
        result = {
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": cleaned_question,
            "chat_reply": None,
            "reasoning": reasoning,
            "clarification_question": clarification_question.strip(),
            "clarification_options": grounded_options,
            "clarification_topic": (
                parsed.get("clarification_topic")
                if isinstance(parsed.get("clarification_topic"), str)
                else None
            ),
            "confirmation_complete": parsed.get("confirmation_complete") is True,
        }
        return result

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
            "clarification_question": "",
            "clarification_options": [],
        }

    return {
        "intent": "data",
        "complexity": complexity,
        "rewritten_question": rewritten.strip(),
        "chat_reply": None,
        "reasoning": reasoning,
        "clarification_question": "",
        "clarification_options": [],
    }


def _clarification_phrase(question: str) -> str:
    match = re.search(r"[\"'“‘]([^\"'”’]+)[\"'”’]", question)
    return match.group(1).strip() if match else ""


def _gate_classifier_clarification(
    result: Dict[str, Any],
    asked_items: Sequence[str],
) -> Dict[str, Any]:
    if result.get("intent") != "clarification":
        return result

    result = _enforce_allowed_clarification_topic(result)
    if result.get("intent") != "clarification":
        return result

    phrase = _clarification_phrase(result["clarification_question"])
    catalog_tokens = frozenset()
    if phrase or asked_items:
        try:
            catalog_tokens = frozenset(
                item["value"]
                for item in get_business_value_catalog()
                if isinstance(item.get("value"), str)
            )
        except ValueIndexError as exc:
            raise ClassificationError(
                "The business category catalog is unavailable; please try again later."
            ) from exc

    askable, reason = is_askable(
        phrase,
        asked_items,
        catalog_tokens=catalog_tokens,
    )
    if askable:
        return result

    logger.info("classifier clarification rejected reason=%s", reason)
    if reason == "junk":
        return {
            **result,
            "intent": "data",
            "rewritten_question": result["original_question"],
            "chat_reply": "",
            "clarification_question": "",
            "clarification_options": [],
            "reasoning": "Ignored a clarification phrase with no business meaning.",
        }
    return {
        **result,
        "intent": "chat",
        "rewritten_question": result["original_question"],
        "chat_reply": (
            "I already asked about that. Please rephrase your request."
        ),
        "clarification_question": "",
        "clarification_options": [],
        "reasoning": "A classifier clarification repeated an already asked item.",
    }


def _enforce_allowed_clarification_topic(
    result: Dict[str, Any],
) -> Dict[str, Any]:
    topic = result.get("clarification_topic")
    if (
        result.get("intent") != "clarification"
        or (
            isinstance(topic, str)
            and topic in ALLOWED_CLARIFICATION_TOPICS
        )
    ):
        return result

    logger.info("classifier_clarification_rejected=1 reason=topic_not_allowed")
    original_question = result.get("original_question") or result.get(
        "rewritten_question", ""
    )
    return {
        **result,
        "intent": "data",
        "rewritten_question": original_question,
        "chat_reply": "",
        "clarification_question": "",
        "clarification_options": [],
        "clarification_topic": None,
        "reasoning": (
            "The requested clarification topic is not allowed; sending the "
            "question to SQL generation."
        ),
    }


async def classify_and_rewrite(
    user_question: str,
    prior_context: list[Dict[str, Any]] | None = None,
    asked_items: Sequence[str] = (),
    selected_catalog_filters: Sequence[Dict[str, Any]] = (),
    cache_question: str | None = None,
) -> Dict[str, Any]:
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
    if selected_catalog_filters:
        logger.info(
            "Classifier using %d preselected catalog filter(s)",
            len(selected_catalog_filters),
        )
        return {
            "original_question": cleaned_question,
            "rewritten_question": cleaned_question,
            "cache_question": cache_question or cleaned_question,
            "intent": "data",
            "complexity": "simple",
            "chat_reply": "",
            "reasoning": "A pending clarification option supplied the catalog filter.",
            "clarification_question": "",
            "clarification_options": [],
            "catalog_filters": [dict(item) for item in selected_catalog_filters],
            "catalog_filter_combiner": "AND",
        }

    context = bound_classifier_context(prior_context)
    has_reference = _has_context_reference(cleaned_question)
    if has_reference and not context:
        result = {
            "original_question": cleaned_question,
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": cleaned_question,
            "chat_reply": None,
            "reasoning": "The question refers to prior context that is not available.",
            "clarification_question": (
                "What result, entity, or time period are you referring to?"
            ),
            "clarification_options": [],
            "clarification_topic": "entity",
        }
        return _gate_classifier_clarification(result, asked_items)
    ambiguous_options = _ambiguous_metric_options(cleaned_question, context)
    if ambiguous_options:
        options_text = ", ".join(ambiguous_options)
        result = {
            "original_question": cleaned_question,
            "intent": "clarification",
            "complexity": "simple",
            "rewritten_question": cleaned_question,
            "chat_reply": None,
            "reasoning": "The prior result contains multiple possible metrics.",
            "clarification_question": (
                f"Which result do you mean: {options_text}?"
            ),
            "clarification_options": ambiguous_options,
            "clarification_topic": None,
        }
        result = _gate_classifier_clarification(result, asked_items)
        return {"original_question": cleaned_question, **result}

    try:
        business_request = resolve_business_request(
            cleaned_question,
            asked_items=asked_items,
        )
    except ValueIndexError as exc:
        logger.error("Business-type resolution failed: %s", exc)
        raise ClassificationError(
            "The business category catalog is unavailable; please try again later."
        ) from exc
    if business_request is not None:
        result = {
            "original_question": cleaned_question,
            "intent": business_request["intent"],
            "complexity": "simple",
            "rewritten_question": business_request["rewritten_question"],
            "chat_reply": business_request.get("chat_reply", ""),
            "reasoning": business_request["reasoning"],
            "clarification_question": business_request["clarification_question"],
            "clarification_options": business_request["clarification_options"],
            "catalog_filters": business_request["catalog_filters"],
        }
        if result["intent"] == "clarification":
            result["clarification_topic"] = "shop_type"
        if "clarification_kind" in business_request:
            result["clarification_kind"] = business_request[
                "clarification_kind"
            ]
        if "confirmation_complete" in business_request:
            result["confirmation_complete"] = (
                business_request["confirmation_complete"] is True
            )
        if "catalog_filter_combiner" in business_request:
            result["catalog_filter_combiner"] = business_request[
                "catalog_filter_combiner"
            ]
        return result

    # When off, the LLM is still used for intent and complexity, but its
    # rewrite is ignored so invented database values never reach later nodes.
    use_rewrite = os.getenv("USE_QUESTION_REWRITE", "true").strip().lower() in {
        "1", "true", "yes",
    }

    llm = get_llm_service()
    messages = build_classifier_messages(cleaned_question, context)

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
            "clarification_question": "",
            "clarification_options": [],
        }
    except LLMServiceError as e:
        raise ClassificationError(f"LLM call failed during classification: {e}") from e

    normalized = _normalize_llm_output(parsed, cleaned_question, context)
    if (
        normalized["intent"] == "clarification"
        and not has_reference
        and (
            _SELF_CONTAINED_DATA_REQUEST_PATTERN.search(cleaned_question)
            or _SHOP_STATUS_COMPARISON_PATTERN.search(cleaned_question)
        )
    ):
        standalone_question = re.split(
            r"\n\s*Clarification:", cleaned_question, maxsplit=1, flags=re.IGNORECASE
        )[0].strip()
        normalized.update({
            "intent": "data",
            "complexity": "complex",
            "rewritten_question": standalone_question,
            "chat_reply": "",
            "clarification_question": "",
            "clarification_options": [],
            "clarification_topic": None,
            "reasoning": (
                "The question contains a complete data request and does not "
                "require a city filter unless one was explicitly requested."
            ),
        })
    if (
        normalized["intent"] == "clarification"
        and _BOOKING_REVENUE_CATEGORY_PATTERN.search(cleaned_question)
        and not re.search(r"\bplan\s+categor", cleaned_question, re.IGNORECASE)
    ):
        normalized.update({
            "intent": "data",
            "rewritten_question": cleaned_question,
            "chat_reply": "",
            "clarification_question": "",
            "clarification_options": [],
            "clarification_topic": None,
            "reasoning": (
                "Booking revenue is grouped by the category of the associated shop."
            ),
        })
    normalized = _gate_classifier_clarification(normalized, asked_items)
    if normalized["intent"] == "data":
        if has_reference and context:
            context_note = _verified_followup_context_note(context)
            if context_note:
                normalized["rewritten_question"] = (
                    normalized["rewritten_question"].rstrip()
                    + context_note
                )
        elif not use_rewrite:
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