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
from app.agent.catalog_resolver import resolve_structured_shop_types
from app.agent.nodes.business_rules import (
    BusinessRulesError,
    find_rules_by_terms,
    get_business_rule_catalog,
)
from app.services.date_resolver import DateResolutionError, resolve_temporal_intents
from app.services.value_index import (
    ValueIndexError,
    get_business_value_catalog,
    shop_name_matches_term,
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
    uses_prior_context = parsed.get("uses_prior_context", False)
    if not isinstance(uses_prior_context, bool):
        raise ClassificationError("uses_prior_context must be a boolean.")
    if uses_prior_context and not prior_context:
        raise ClassificationError(
            "uses_prior_context cannot be true when no prior context was supplied."
        )
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
            "uses_prior_context": uses_prior_context,
            "temporal_intents": _structured_list(parsed, "temporal_intents"),
            "shop_type_entities": _structured_list(parsed, "shop_type_entities"),
            "matched_business_rule_terms": _structured_list(
                parsed, "matched_business_rule_terms"
            ),
            "catalog_filter_combiner": _structured_combiner(parsed),
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
            "uses_prior_context": False,
        }

    return {
        "intent": "data",
        "complexity": complexity,
        "rewritten_question": rewritten.strip(),
        "chat_reply": None,
        "reasoning": reasoning,
        "clarification_question": "",
        "clarification_options": [],
        "uses_prior_context": uses_prior_context,
        "temporal_intents": _structured_list(parsed, "temporal_intents"),
        "shop_type_entities": _structured_list(parsed, "shop_type_entities"),
        "matched_business_rule_terms": _structured_list(
            parsed, "matched_business_rule_terms"
        ),
        "catalog_filter_combiner": _structured_combiner(parsed),
    }


def _structured_list(parsed: Dict[str, Any], key: str) -> list:
    value = parsed.get(key, [])
    if not isinstance(value, list):
        raise ClassificationError(f"LLM field {key!r} must be a list.")
    if key in parsed and not value:
        return []
    if key == "matched_business_rule_terms" and any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ClassificationError(
            "matched_business_rule_terms must contain non-empty strings."
        )
    return value


def _structured_combiner(parsed: Dict[str, Any]) -> str | None:
    value = parsed.get("catalog_filter_combiner")
    if value is None:
        return None
    if value not in {"AND", "OR"}:
        raise ClassificationError(
            "catalog_filter_combiner must be either AND or OR."
        )
    return value


def _validate_structured_classifier_output(
    normalized: Dict[str, Any],
    business_value_catalog: list[dict[str, Any]],
    business_rule_catalog: list[dict[str, Any]],
    original_question: str,
) -> Dict[str, Any]:
    if normalized["intent"] not in {"data", "clarification"}:
        return normalized

    try:
        shop_type_result = resolve_structured_shop_types(
            normalized.get("shop_type_entities", []),
            business_value_catalog,
            normalized.get("catalog_filter_combiner"),
        )
        if shop_type_result is not None:
            normalized.update(shop_type_result)
            if normalized["intent"] == "clarification":
                normalized["clarification_topic"] = "shop_type"
                normalized["clarification_kind"] = "choose_one"
        elif (
            normalized["intent"] == "clarification"
            and normalized.get("clarification_topic") == "shop_type"
        ):
            normalized.update({
                "intent": "data",
                "rewritten_question": original_question,
                "chat_reply": "",
                "clarification_question": "",
                "clarification_options": [],
                "reasoning": (
                    "No structured shop type was extracted; continue to query generation."
                ),
            })

        selected_terms = normalized.get("matched_business_rule_terms", [])
        valid_rule_terms = {item["term"] for item in business_rule_catalog}
        unknown_terms = set(selected_terms) - valid_rule_terms
        if unknown_terms:
            raise ClassificationError(
                "Classifier selected business-rule identifiers outside the catalog: "
                f"{sorted(unknown_terms)}."
            )
        temporal_result = resolve_temporal_intents(
            normalized.get("temporal_intents", [])
        )
        selected_rules = find_rules_by_terms(selected_terms)
        state_filters_by_term = {
            rule["term"]: rule["query_logic"]["state_filter"]
            for rule in selected_rules
            if isinstance(rule.get("query_logic"), dict)
            and isinstance(rule["query_logic"].get("state_filter"), dict)
        }
        selected_term_set = set(selected_terms)
        validated_state_filters = []
        for item in temporal_result["current_state_filters"]:
            term = item["business_rule_term"]
            if term not in selected_term_set:
                raise ClassificationError(
                    "Current-state intent is missing its selected business rule: "
                    f"{term!r}."
                )
            state_filter = state_filters_by_term.get(term)
            if state_filter is None or any(
                item.get(key) != state_filter.get(key)
                for key in ("entity", "state")
            ):
                raise ClassificationError(
                    "Current-state intent does not match its catalog state filter: "
                    f"{term!r}."
                )
            validated_state_filters.append({**item, **state_filter})
        normalized["resolved_temporal_constraints"] = temporal_result["date_ranges"]
        normalized["current_state_filters"] = validated_state_filters
        normalized["matched_business_rule_terms"] = selected_terms
        normalized["structured_catalog_validated"] = True
    except (ValueError, DateResolutionError) as exc:
        raise ClassificationError(
            f"Structured temporal or shop-type output failed validation: {exc}"
        ) from exc
    except BusinessRulesError as exc:
        raise ClassificationError(
            f"Structured business-rule output failed validation: {exc}"
        ) from exc

    return normalized


def _clarification_phrase(question: str) -> str:
    match = re.search(r"[\"'“‘]([^\"'”’]+)[\"'”’]", question)
    return match.group(1).strip() if match else ""


def _gate_classifier_clarification(
    result: Dict[str, Any],
    asked_items: Sequence[str],
) -> Dict[str, Any]:
    if result.get("intent") != "clarification":
        return result

    return _enforce_allowed_clarification_topic(result)


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
    # When off, the LLM is still used for intent and complexity, but its
    # rewrite is ignored so invented database values never reach later nodes.
    use_rewrite = os.getenv("USE_QUESTION_REWRITE", "true").strip().lower() in {
        "1", "true", "yes",
    }

    llm = get_llm_service()
    try:
        business_value_catalog = [
            {
                "level": (
                    "category"
                    if item.get("source_table") == "categories"
                    else "subcategory"
                ),
                "value": item["value"],
            }
            for item in get_business_value_catalog()
            if isinstance(item.get("value"), str)
            and item.get("source_table") in {"categories", "subcategories"}
        ]
        business_rule_catalog = get_business_rule_catalog()
    except (ValueIndexError, BusinessRulesError) as exc:
        raise ClassificationError(
            "The business value or rule catalog is unavailable."
        ) from exc
    messages = build_classifier_messages(
        cleaned_question,
        context,
        business_rule_terms=[
            item["term"]
            for item in business_rule_catalog
            if isinstance(item.get("term"), str)
        ],
    )

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

    for attempt in range(2):
        try:
            normalized = _normalize_llm_output(parsed, cleaned_question, context)
            quoted_entity = _clarification_phrase(cleaned_question)
            if (
                normalized["intent"] == "clarification"
                and normalized.get("clarification_topic") == "entity"
                and quoted_entity
                and shop_name_matches_term(quoted_entity)
            ):
                normalized.update({
                    "intent": "data",
                    "rewritten_question": cleaned_question,
                    "chat_reply": "",
                    "clarification_question": "",
                    "clarification_options": [],
                    "clarification_topic": None,
                    "reasoning": (
                        "The quoted entity matches an indexed shop name; "
                        "continue to query generation."
                    ),
                })
            normalized = _validate_structured_classifier_output(
                normalized,
                business_value_catalog,
                business_rule_catalog,
                cleaned_question,
            )
            break
        except ClassificationError as exc:
            if attempt:
                raise
            logger.warning(
                "Classifier structured output failed validation; requesting one correction: %s",
                exc,
            )
            repair_messages = [
                messages[0],
                {"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)},
                {
                    "role": "user",
                    "content": (
                        "The previous JSON response failed validation. "
                        "Correct that response using the exact validation error "
                        "below. Preserve its meaning and return only a complete "
                        "JSON object matching the system schema. Do not invent "
                        "catalog values or rule identifiers.\n"
                        f"Validation error: {exc}"
                    ),
                },
            ]
            try:
                parsed = await llm.call_agent_json(
                    "CLASSIFIER", repair_messages, temperature=0.0
                )
            except LLMServiceError as repair_error:
                raise ClassificationError(
                    f"Classifier structured-output correction failed: {repair_error}"
                ) from repair_error
    normalized = _gate_classifier_clarification(normalized, asked_items)
    if normalized["intent"] == "data":
        if normalized.get("uses_prior_context") and context:
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