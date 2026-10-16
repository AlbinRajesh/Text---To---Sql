"""
chat.py — POST /api/chat

Exposes the LangGraph pipeline to the frontend.

Safety rules for this file:
  - Input is validated (non-empty, length-capped) before any LLM is called.
  - Internal details (SQL, schema, stack traces, escalation reasons) are
    NEVER returned to the client. They stay in the server log and in
    logs/escalations.jsonl, matched to the user's report by request_id.
  - The whole pipeline run has a hard time limit.
"""

import asyncio
import logging
import re
import uuid
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

import json
from fastapi.responses import StreamingResponse
from app.services.timing import set_emitter

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.agent.graph import run_pipeline
from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.run_self_fix import QueryExecutionError, run_query
from app.agent.nodes.schema_grounding import get_cached_schema
from app.services.pending_interactions import (
    consume_pending_interaction_if_matches,
    consume_pending_interaction,
    get_pending_interaction,
    store_pending_interaction,
    update_pending_interaction,
)
from app.services.clarification_gate import normalize_phrase
from app.services.value_index import ValueIndexError, get_business_value_catalog
from app.services.safe_user_sql import (
    UserSQLValidationError,
    execute_user_select,
    validate_user_select,
)
from app.services.suggestion_store import (
    SuggestionNotFoundError,
    SuggestionStoreUnavailableError,
    clear_suggestion_plans,
    consume_suggestion_plan,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

MAX_QUESTION_LENGTH = 1000
PIPELINE_TIMEOUT_SECONDS = 90

GENERIC_ERROR = "Something went wrong on our side. Please try again."
TIMEOUT_ERROR = "This is taking too long. Please try again."
PIPELINE_ORDER = [
    "classify", "cache_check", "grade_schema", "business_rules",
    "generate_query", "safety_check", "execute", "result_grader", "return_save",
]
_CLARIFICATION_REPLY_RULES = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "rules"
        / "clarification_words.json"
    ).read_text(encoding="utf-8")
)["en"]
_NEW_QUESTION_STARTERS = tuple(
    _CLARIFICATION_REPLY_RULES["new_question_starters"]
)
_TYPO_MATCH_THRESHOLD = float(
    _CLARIFICATION_REPLY_RULES["typo_match_threshold"]
)
_TYPO_MATCH_MARGIN = float(
    _CLARIFICATION_REPLY_RULES["typo_match_margin"]
)

class ChatRequest(BaseModel):
    question: str = Field(..., max_length=MAX_QUESTION_LENGTH)
    conversation_id: Optional[UUID] = None

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question must not be empty.")
        return value


class ValueSuggestion(BaseModel):
    column: str
    original: str
    suggested: str
    suggestion_id: Optional[str] = None


class SuggestionRedemptionRequest(BaseModel):
    conversation_id: UUID
    suggestion_id: str = Field(..., min_length=32, max_length=128)


class ChatResponse(BaseModel):
    request_id: str
    conversation_id: Optional[str] = None
    status: Literal[
        "ok", "unanswerable", "escalated", "chat", "clarification", "confirmation"
    ]
    fast_path: bool = False
    results: Optional[List[Dict[str, Any]]] = None
    columns: Optional[List[str]] = None
    column_formats: Dict[str, Literal["identifier"]] = Field(default_factory=dict)
    suggestions: List[ValueSuggestion] = Field(default_factory=list)
    count: Optional[int] = None
    row_count: Optional[int] = None
    answer_type: Optional[Literal["single_value", "table", "empty", "unanswerable"]] = None
    single_value: Any = None
    column_labels: Optional[Dict[str, str]] = None
    summary: Optional[str] = None
    truncated: bool = False
    row_limit: Optional[int] = None
    no_data_reason: Optional[Literal["no_rows", "all_null"]] = None
    matching_row_count: Optional[int] = None
    message: Optional[str] = None
    clarification_options: List[str] = Field(default_factory=list)
    confirmation_sql: Optional[str] = None
    cache_hit: bool = False


def _build_response(
    state: Dict[str, Any],
    request_id: str,
    conversation_id: Optional[str] = None,
) -> ChatResponse:
    final_answer = state.get("final_answer") or {}

    if state.get("escalated"):
        return ChatResponse(
            request_id=request_id,
            conversation_id=conversation_id or state.get("conversation_id"),
            status="escalated",
            message=final_answer.get("error") or GENERIC_ERROR,
        )

    if "clarification_question" in final_answer:
        return ChatResponse(
            request_id=request_id,
            conversation_id=conversation_id or state.get("conversation_id"),
            status="clarification",
            message=final_answer["clarification_question"],
            clarification_options=final_answer.get("clarification_options", []),
        )

    if "chat_reply" in final_answer:
        return ChatResponse(
            request_id=request_id,
            conversation_id=conversation_id or state.get("conversation_id"),
            status="chat",
            message=final_answer["chat_reply"],
        )

    if "results" in final_answer:
        message = final_answer.get("message")
        widening_note = _widening_note(state)
        if widening_note:
            message = f"{message.rstrip()} {widening_note}" if message else widening_note
        return ChatResponse(
            request_id=request_id,
            conversation_id=conversation_id or state.get("conversation_id"),
            status="ok",
            results=final_answer.get("results", []),
            columns=final_answer.get("columns", []),
            column_formats=final_answer.get("column_formats", {}),
            suggestions=final_answer.get("suggestions", []),
            count=final_answer.get("count", 0),
            row_count=final_answer.get("row_count", final_answer.get("count", 0)),
            answer_type=final_answer.get("answer_type"),
            single_value=final_answer.get("single_value"),
            column_labels=final_answer.get("column_labels", {}),
            summary=final_answer.get("summary"),
            truncated=bool(final_answer.get("truncated", False)),
            row_limit=final_answer.get("row_limit"),
            no_data_reason=final_answer.get("no_data_reason"),
            matching_row_count=final_answer.get("matching_row_count"),
            message=message,
            cache_hit=bool(state.get("cache_hit")),
        )

    # Node 6 said the question can't be answered from this database.
    return ChatResponse(
        request_id=request_id,
        conversation_id=conversation_id or state.get("conversation_id"),
        status="unanswerable",
        answer_type="unanswerable",
        message=final_answer.get("reasoning")
        or "This question can't be answered from the available data.",
        cache_hit=bool(state.get("cache_hit")),
    )


def _widening_note(state: Dict[str, Any]) -> Optional[str]:
    unclear_phrase = state.get("widening_unclear_phrase")
    chosen_value = state.get("widening_chosen_value")
    if (
        not isinstance(unclear_phrase, str)
        or not unclear_phrase.strip()
        or not isinstance(chosen_value, str)
        or not chosen_value.strip()
    ):
        return None

    normalized_phrase = normalize_phrase(unclear_phrase)
    normalized_value = normalize_phrase(chosen_value)
    if (
        not normalized_phrase
        or not normalized_value
        or normalized_phrase == normalized_value
        or f" {normalized_phrase} " in f" {normalized_value} "
    ):
        return None

    logger.info("widening_note_added=1")
    return (
        f"Showing shops classified under {chosen_value.strip()}. "
        f"This doesn't show which shops sell {unclear_phrase.strip()}."
    )


def _build_error_response(request_id: str, message: str) -> Dict[str, Any]:
    """Keep transport-level failures on the same answer contract as pipeline results."""
    return ChatResponse(
        request_id=request_id,
        status="escalated",
        message=message,
    ).model_dump()


_SELECT_SQL_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_UNSUPPORTED_SQL_START = re.compile(
    r"^\s*(?:"
    r"INSERT\s+INTO\b|"
    r"UPDATE\s+[`\"A-Za-z0-9_.]+\s+SET\b|"
    r"DELETE\s+FROM\b|"
    r"REPLACE\s+INTO\b|"
    r"CREATE\s+(?:TEMPORARY\s+)?(?:TABLE|DATABASE|VIEW|INDEX|USER)\b|"
    r"ALTER\s+(?:TABLE|DATABASE|USER)\b|"
    r"DROP\s+(?:TABLE|DATABASE|VIEW|INDEX|USER)\b|"
    r"TRUNCATE(?:\s+TABLE)?\b|"
    r"GRANT\b.*\bON\b|REVOKE\b.*\bON\b|"
    r"SHOW\s+(?:TABLES|DATABASES|COLUMNS|CREATE\s+TABLE|INDEX)\b|"
    r"(?:DESCRIBE|DESC)\s+[`\"A-Za-z0-9_.]+\b|"
    r"EXPLAIN\s+(?:SELECT|WITH)\b|"
    r"CALL\s+[`\"A-Za-z0-9_.]+\s*\(|"
    r"USE\s+[`\"A-Za-z0-9_]+\s*;?\s*$|"
    r"SET\s+(?:@@?[\w.]+|[`\"A-Za-z_]\w*)\s*=|"
    r"LOAD\s+DATA\b|RENAME\s+TABLE\b|"
    r"ANALYZE\s+TABLE\b|OPTIMIZE\s+TABLE\b|REPAIR\s+TABLE\b|"
    r"FLUSH\s+\w+\b|KILL\s+(?:QUERY\s+)?\d+\b|"
    r"START\s+TRANSACTION\b|BEGIN(?:\s+TRANSACTION)?\s*;?\s*$|"
    r"COMMIT\s*;?\s*$|ROLLBACK\s*;?\s*$"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_AFFIRMATIVE = {
    "yes", "yes please", "yeah", "yep", "run it", "execute it",
    "go ahead", "confirm", "confirm run",
}
_NEGATIVE = {"no", "no thanks", "cancel", "dont run", "do not run"}


def _is_affirmative(value: str) -> bool:
    return re.sub(r"[^a-z ]", "", value.casefold()).strip() in _AFFIRMATIVE


def _is_negative(value: str) -> bool:
    return re.sub(r"[^a-z ]", "", value.casefold()).strip() in _NEGATIVE


def _matched_clarification_option(
    value: str,
    options: List[Any],
) -> Optional[str]:
    normalized = _normalize_clarification_reply(value)
    for option in options:
        label = option.get("label") if isinstance(option, dict) else option
        if not isinstance(label, str):
            continue
        if normalized == _normalize_clarification_reply(label):
            return label
    return None


def _fuzzy_clarification_option(
    value: str,
    options: List[Any],
) -> Optional[str]:
    labels = [
        option.get("label") if isinstance(option, dict) else option
        for option in options
    ]
    labels = [label for label in labels if isinstance(label, str)]
    if not 2 <= len(labels) <= 10:
        return None

    normalized = _normalize_clarification_reply(value)
    ranked = sorted(
        [
            (
                SequenceMatcher(
                    None,
                    normalized,
                    _normalize_clarification_reply(label),
                ).ratio(),
                label,
            )
            for label in labels
        ],
        reverse=True,
    )
    best_score, best_label = ranked[0]
    second_score = ranked[1][0]
    if best_score < _TYPO_MATCH_THRESHOLD:
        logger.info("clarification typo_match rejected reason=below_threshold")
        return None
    if best_score - second_score < _TYPO_MATCH_MARGIN:
        logger.info("clarification typo_match rejected reason=insufficient_margin")
        return None
    logger.info("clarification reply handled rule=typo_match")
    return best_label


def _normalize_clarification_reply(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    return re.sub(r"[^\w\s]+$", "", normalized).rstrip()


def _strip_leading_yes_no(value: str) -> str:
    return re.sub(
        r"^\s*(?:yes|no)\b\s*[,;:.-]*\s*",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    ).strip()


def _is_new_clarification_question(value: str) -> bool:
    normalized = _normalize_clarification_reply(value)
    if not normalized:
        return False
    starter_pattern = r"^(?:" + "|".join(
        re.escape(starter) for starter in _NEW_QUESTION_STARTERS
    ) + r")\b"
    return (
        len(normalized) > 40
        or normalized.endswith("?")
        or bool(re.match(starter_pattern, normalized, re.IGNORECASE))
    )


def _clarification_requires_selection(value: str) -> bool:
    return bool(
        re.search(r"\b(?:which|what)\b|\bor\b", value, re.IGNORECASE)
    )


def _clarification_kind(question: str, options: List[str]) -> str:
    normalized = question.strip().casefold()
    if re.search(r"\b(?:both|all of these|any combination)\b", normalized):
        return "choose-several"
    if re.match(
        r"^(?:do|does|did|is|are|was|were|can|could|would|should|will)\b",
        normalized,
    ):
        return "yes/no"
    return "choose-one"


def _clarification_phrase(question: str) -> str:
    quoted = re.search(r"[\"'“‘]([^\"'”’]+)[\"'”’]", question)
    return quoted.group(1).strip() if quoted else ""


def _resumed_asked_items(
    pending: Optional[Dict[str, Any]],
    original_question: str,
    direct_response: Optional[ChatResponse],
    pipeline_question: str,
) -> List[str]:
    if (
        direct_response is not None
        or pipeline_question == original_question
        or pending is None
        or pending.get("type") != "clarification"
    ):
        return []
    asked_items = pending.get("payload", {}).get("asked_items", [])
    return [
        item for item in asked_items if isinstance(item, str)
    ] if isinstance(asked_items, list) else []


def _resumed_catalog_filters(
    pending: Optional[Dict[str, Any]],
    reply: str,
    pipeline_question: str,
) -> List[Dict[str, Any]]:
    record = _resumed_catalog_option(pending, reply, pipeline_question)
    if (
        record is None
        or record.get("level") not in {"category", "subcategory"}
        or not isinstance(record.get("value"), str)
    ):
        return []
    return [{
        "level": record["level"],
        "value": record["value"],
        "exclude": False,
    }]


def _resumed_catalog_option(
    pending: Optional[Dict[str, Any]],
    reply: str,
    pipeline_question: str,
) -> Optional[Dict[str, Any]]:
    if (
        pending is None
        or pending.get("type") != "clarification"
        or "\nClarification: The user selected: " not in pipeline_question
    ):
        return None

    payload = pending.get("payload", {})
    options = payload.get("options", [])
    selected = _matched_clarification_option(reply, options)
    if selected is None:
        selected = _fuzzy_clarification_option(reply, options)
    if selected is None:
        return None

    return next(
        (
            option for option in options
            if isinstance(option, dict) and option.get("label") == selected
        ),
        None,
    )


def _resumed_widening_context(
    pending: Optional[Dict[str, Any]],
    reply: str,
    pipeline_question: str,
) -> tuple[Optional[str], Optional[str]]:
    record = _resumed_catalog_option(pending, reply, pipeline_question)
    if record is None or not isinstance(record.get("value"), str):
        return None, None
    unclear_phrase = pending.get("payload", {}).get("unclear_phrase")
    return (
        unclear_phrase if isinstance(unclear_phrase, str) and unclear_phrase else None,
        record["value"],
    )


def _catalog_option_records(options: List[str]) -> List[Dict[str, Any]]:
    try:
        catalog = get_business_value_catalog()
    except ValueIndexError:
        logger.warning(
            "Value catalog unavailable while saving clarification options; "
            "preserving labels without catalog levels."
        )
        catalog = []
    records = []
    for option in options:
        explicit_level = None
        value = option
        prefix = re.match(r"^(Category|Subcategory):\s*(.+)$", option, re.IGNORECASE)
        if prefix:
            explicit_level = prefix.group(1).casefold()
            value = prefix.group(2).strip()
        matches = [
            item for item in catalog
            if isinstance(item.get("value"), str)
            and item["value"].casefold() == value.casefold()
            and (
                explicit_level is None
                or (
                    explicit_level == "category"
                    and item.get("source_table") == "categories"
                )
                or (
                    explicit_level == "subcategory"
                    and item.get("source_table") == "subcategories"
                )
            )
        ]
        if matches:
            for item in matches:
                level = (
                    "category"
                    if item["source_table"] == "categories"
                    else "subcategory"
                )
                records.append({
                    "label": option,
                    "value": item["value"],
                    "level": level,
                })
        else:
            records.append({
                "label": option,
                "value": value,
                "level": explicit_level,
            })
    return records


def _clarification_options_for_response(options: List[Any]) -> List[str]:
    return [
        option["label"] if isinstance(option, dict) else option
        for option in options
        if isinstance(option, str)
        or (isinstance(option, dict) and isinstance(option.get("label"), str))
    ]


def _stale_clarification_response(
    request_id: str,
    conversation_id: str,
) -> ChatResponse:
    return ChatResponse(
        request_id=request_id,
        conversation_id=conversation_id,
        status="chat",
        message="This clarification was already handled or has expired. Please send your request again.",
    )


async def _handle_pending_or_sql(
    question: str,
    request_id: str,
    conversation_id: str,
) -> tuple[Optional[ChatResponse], str]:
    """Handle safe SQL confirmation and clarification continuation before classification."""
    pending = get_pending_interaction(conversation_id)
    clarification_dropped = False
    if pending is not None and pending["type"] == "clarification":
        payload = pending["payload"]
        options = payload.get("options", [])
        option = _matched_clarification_option(question, options)
        kind = payload.get(
            "kind",
            "choose-one"
            if _clarification_requires_selection(
                payload.get("clarification_question", "")
            )
            else "yes/no",
        )
        affirmative = _is_affirmative(question)
        negative = _is_negative(question)
        if option is not None:
            if not consume_pending_interaction_if_matches(
                conversation_id,
                pending["interaction_id"],
                pending["version"],
            ):
                return _stale_clarification_response(
                    request_id, conversation_id
                ), question
            return (
                None,
                f"{payload.get('original_question', payload.get('question', ''))}"
                f"\nClarification: The user selected: {option}.",
            )

        new_question = _strip_leading_yes_no(question)
        if _is_new_clarification_question(new_question):
            if not consume_pending_interaction_if_matches(
                conversation_id,
                pending["interaction_id"],
                pending["version"],
            ):
                return _stale_clarification_response(
                    request_id, conversation_id
                ), question
            logger.info("clarification reply handled rule=new_question")
            question = new_question
            clarification_dropped = True
        elif (option := _fuzzy_clarification_option(question, options)) is not None:
            if not consume_pending_interaction_if_matches(
                conversation_id,
                pending["interaction_id"],
                pending["version"],
            ):
                return _stale_clarification_response(
                    request_id, conversation_id
                ), question
            return (
                None,
                f"{payload.get('original_question', payload.get('question', ''))}"
                f"\nClarification: The user selected: {option}.",
            )
        elif kind == "yes/no" and affirmative:
            if payload.get("confirmation_complete") is True:
                if not consume_pending_interaction_if_matches(
                    conversation_id,
                    pending["interaction_id"],
                    pending["version"],
                ):
                    return _stale_clarification_response(
                        request_id, conversation_id
                    ), question
                return (
                    None,
                    f"{payload.get('original_question', payload.get('question', ''))}"
                    "\nClarification: The user confirmed: yes.",
                )
            logger.info(
                "clarification reply invalid reason=confirmation_missing_filter"
            )
        elif kind == "yes/no" and negative:
            if not consume_pending_interaction_if_matches(
                conversation_id,
                pending["interaction_id"],
                pending["version"],
            ):
                return _stale_clarification_response(
                    request_id, conversation_id
                ), question
            return (
                ChatResponse(
                    request_id=request_id,
                    conversation_id=conversation_id,
                    status="chat",
                    message=(
                        "Cancelled. Please rephrase your request if you'd like "
                        "to try again."
                    ),
                ),
                question,
            )
        else:
            logger.info("clarification reply invalid reason=no_match")

        # Rule 4 (typo matching) is added separately; until then every unmatched
        # reply stays on this note as an invalid reply.
        if not _is_new_clarification_question(new_question):
            invalid_replies = int(payload.get("invalid_replies", 0)) + 1
            logger.info(
                "clarification reply invalid_count=%d",
                invalid_replies,
            )
            if invalid_replies >= 2:
                consumed = consume_pending_interaction_if_matches(
                    conversation_id,
                    pending["interaction_id"],
                    pending["version"],
                )
                if not consumed:
                    return _stale_clarification_response(
                        request_id, conversation_id
                    ), question
                return (
                    ChatResponse(
                        request_id=request_id,
                        conversation_id=conversation_id,
                        status="chat",
                        message=(
                            "I couldn't resolve your answer. Please start a new "
                            "request with the shop type or choice you mean."
                        ),
                    ),
                    question,
                )
            updated_payload = {
                **payload,
                "invalid_replies": invalid_replies,
            }
            if not update_pending_interaction(
                conversation_id,
                pending["interaction_id"],
                pending["version"],
                updated_payload,
            ):
                return _stale_clarification_response(
                    request_id, conversation_id
                ), question
            return (
                ChatResponse(
                    request_id=request_id,
                    conversation_id=conversation_id,
                    status="clarification",
                    message=payload.get("clarification_question", ""),
                    clarification_options=_clarification_options_for_response(
                        options
                    ),
                ),
                question,
            )

    pending = (
        None
        if clarification_dropped
        else consume_pending_interaction(conversation_id)
    )
    if pending is not None:
        payload = pending["payload"]
        if pending["type"] == "sql_confirmation":
            if _is_affirmative(question):
                try:
                    result = await execute_user_select(
                        payload["sql"],
                        get_cached_schema(),
                    )
                except UserSQLValidationError as exc:
                    return (
                        ChatResponse(
                            request_id=request_id,
                            conversation_id=conversation_id,
                            status="chat",
                            message=f"The query was not run: {exc}",
                        ),
                        question,
                    )
                return (
                    ChatResponse(
                        request_id=request_id,
                        conversation_id=conversation_id,
                        status="ok",
                        results=result["results"],
                        columns=result["columns"],
                        count=result["count"],
                        row_count=result["row_count"],
                        answer_type="table" if result["results"] else "empty",
                        truncated=result["truncated"],
                        row_limit=result["row_limit"],
                    ),
                    question,
                )
            if _is_negative(question):
                return (
                    ChatResponse(
                        request_id=request_id,
                        conversation_id=conversation_id,
                        status="chat",
                        message="Cancelled. No SQL was run.",
                    ),
                    question,
                )
        elif pending["type"] == "clarification":
            return _stale_clarification_response(request_id, conversation_id), question

    if _UNSUPPORTED_SQL_START.match(question):
        return (
            ChatResponse(
                request_id=request_id,
                conversation_id=conversation_id,
                status="chat",
                message="Only read-only SELECT queries can be reviewed for execution.",
            ),
            question,
        )

    if _SELECT_SQL_START.match(question):
        try:
            validated_sql = validate_user_select(question, get_cached_schema())
        except UserSQLValidationError as exc:
            return (
                ChatResponse(
                    request_id=request_id,
                    conversation_id=conversation_id,
                    status="chat",
                    message=f"This SQL was not run: {exc}",
                ),
                question,
            )
        store_pending_interaction(
            conversation_id,
            "sql_confirmation",
            {"sql": validated_sql},
        )
        return (
            ChatResponse(
                request_id=request_id,
                conversation_id=conversation_id,
                status="confirmation",
                message="I validated this as a read-only query. Reply yes to run it, or no to cancel.",
                confirmation_sql=validated_sql,
            ),
            question,
        )

    return None, question


def _store_clarification_if_needed(
    state: Dict[str, Any],
    conversation_id: str,
) -> None:
    final_answer = state.get("final_answer") or {}
    if "clarification_question" in final_answer:
        clarification_question = final_answer["clarification_question"]
        raw_options = final_answer.get("clarification_options", [])
        options = (
            [option for option in raw_options if isinstance(option, str)]
            if isinstance(raw_options, list)
            else []
        )
        original_question = state.get("user_question", "")
        if not isinstance(original_question, str):
            original_question = ""
        unclear_phrase = _clarification_phrase(clarification_question)
        # Resolver questions carry a declared kind; only classifier-generated
        # questions need the legacy text-based kind inference.
        clarification_kind = final_answer.get("clarification_kind")
        if clarification_kind in {"choose_one", "choose_several"}:
            clarification_kind = clarification_kind.replace("_", "-")
        elif clarification_kind == "yes/no":
            clarification_kind = "yes/no"
        else:
            clarification_kind = _clarification_kind(
                clarification_question, options
            )
        store_pending_interaction(
            conversation_id,
            "clarification",
            {
                **{
                    "original_question": original_question,
                    "kind": clarification_kind,
                    "unclear_phrase": unclear_phrase,
                    "options": _catalog_option_records(options),
                    "invalid_replies": 0,
                    "asked_items": [unclear_phrase] if unclear_phrase else [],
                    "clarification_question": clarification_question,
                },
                **(
                    {
                        "confirmation_complete":
                            final_answer.get("confirmation_complete") is True
                    }
                    if clarification_kind == "yes/no"
                    else {}
                ),
            },
        )


async def _run_chat_pipeline(
    question: str,
    request_id: str,
    conversation_id: str,
    asked_items: Optional[List[str]] = None,
    catalog_filters: Optional[List[Dict[str, Any]]] = None,
    cache_question: Optional[str] = None,
    widening_unclear_phrase: Optional[str] = None,
    widening_chosen_value: Optional[str] = None,
) -> Dict[str, Any]:
    pipeline_options = {}
    if asked_items:
        pipeline_options["asked_items"] = asked_items
    if catalog_filters:
        pipeline_options["catalog_filters"] = catalog_filters
    if cache_question:
        pipeline_options["cache_question"] = cache_question
    if widening_unclear_phrase is not None:
        pipeline_options["widening_unclear_phrase"] = widening_unclear_phrase
    if widening_chosen_value is not None:
        pipeline_options["widening_chosen_value"] = widening_chosen_value
    state = await run_pipeline(
        question,
        request_id=request_id,
        conversation_id=conversation_id,
        **pipeline_options,
    )
    _store_clarification_if_needed(state, conversation_id)
    return state


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest):
    request_id = str(uuid.uuid4())
    conversation_id = str(body.conversation_id or uuid.uuid4())
    await clear_suggestion_plans(conversation_id)

    try:
        pending = get_pending_interaction(conversation_id)
        direct_response, pipeline_question = await _handle_pending_or_sql(
            body.question,
            request_id,
            conversation_id,
        )
        if direct_response is not None:
            return direct_response
        asked_items = _resumed_asked_items(
            pending, body.question, direct_response, pipeline_question
        )
        catalog_filters = _resumed_catalog_filters(
            pending, body.question, pipeline_question
        )
        widening_unclear_phrase, widening_chosen_value = (
            _resumed_widening_context(
                pending, body.question, pipeline_question
            )
        )
        run_question = (
            pending["payload"].get("original_question", pipeline_question)
            if catalog_filters and pending is not None
            else pipeline_question
        )
        state = await asyncio.wait_for(
            _run_chat_pipeline(
                run_question,
                request_id=request_id,
                conversation_id=conversation_id,
                asked_items=asked_items,
                catalog_filters=catalog_filters,
                cache_question=pipeline_question if catalog_filters else None,
                widening_unclear_phrase=widening_unclear_phrase,
                widening_chosen_value=widening_chosen_value,
            ),
            timeout=PIPELINE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("request_id=%s pipeline timed out after %ss", request_id, PIPELINE_TIMEOUT_SECONDS)
        return JSONResponse(
            status_code=504,
            content=_build_error_response(request_id, TIMEOUT_ERROR),
        )
    except Exception:
        # Full details go to the server log only, never to the client.
        logger.exception("request_id=%s unhandled pipeline error", request_id)
        return JSONResponse(
            status_code=500,
            content=_build_error_response(request_id, GENERIC_ERROR),
        )

    return _build_response(state, request_id, conversation_id)


@router.post("/chat/suggestion", response_model=ChatResponse)
async def redeem_suggestion(body: SuggestionRedemptionRequest):
    """Execute one user-selected, previously validated suggestion plan."""
    request_id = str(uuid.uuid4())
    conversation_id = str(body.conversation_id)

    try:
        plan = await consume_suggestion_plan(
            conversation_id,
            body.suggestion_id,
        )
    except SuggestionNotFoundError:
        return JSONResponse(
            status_code=410,
            content=_build_error_response(
                request_id,
                "This suggestion expired or has already been used. Please ask again.",
            ),
        )
    except SuggestionStoreUnavailableError:
        logger.exception("request_id=%s suggestion store unavailable", request_id)
        return JSONResponse(
            status_code=503,
            content=_build_error_response(
                request_id,
                "Fast suggestion execution is temporarily unavailable. Please ask again.",
            ),
        )

    try:
        execution = await run_query(plan["validated_query"])
        final_answer = _build_result_answer(
            execution,
            plan["generated_query"],
        )
    except QueryExecutionError:
        logger.exception("request_id=%s fast suggestion query failed", request_id)
        return JSONResponse(
            status_code=500,
            content=_build_error_response(request_id, GENERIC_ERROR),
        )
    except Exception:
        logger.exception("request_id=%s failed to build suggestion result", request_id)
        return JSONResponse(
            status_code=500,
            content=_build_error_response(request_id, GENERIC_ERROR),
        )

    response = _build_response(
        {
            "final_answer": final_answer,
            "cache_hit": False,
            "conversation_id": conversation_id,
        },
        request_id,
        conversation_id,
    )
    response.fast_path = True
    return response

@router.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    """
    Same result as /api/chat, but streams live progress as newline-delimited JSON:
      {"type":"node_start","node":"classify"}
      {"type":"node_done","node":"classify","seconds":0.7}
      {"type":"node_skipped","node":"grade_schema"}
      {"type":"result", ...same fields as /api/chat...}
    """
    request_id = str(uuid.uuid4())
    conversation_id = str(body.conversation_id or uuid.uuid4())
    await clear_suggestion_plans(conversation_id)
    queue: asyncio.Queue = asyncio.Queue()
    started: set = set()

    def emit(event: Dict[str, Any]) -> None:
        if event.get("type") == "node_start":
            started.add(event["node"])
        queue.put_nowait(event)

    async def runner() -> None:
        # set_emitter runs inside this task, so the graph's node tasks inherit it.
        set_emitter(emit)
        try:
            pending = get_pending_interaction(conversation_id)
            direct_response, pipeline_question = await _handle_pending_or_sql(
                body.question,
                request_id,
                conversation_id,
            )
            if direct_response is not None:
                for node in PIPELINE_ORDER:
                    queue.put_nowait({"type": "node_skipped", "node": node})
                result = direct_response.model_dump()
                queue.put_nowait({"type": "result", **result})
                queue.put_nowait(None)
                return
            asked_items = _resumed_asked_items(
                pending, body.question, direct_response, pipeline_question
            )
            catalog_filters = _resumed_catalog_filters(
                pending, body.question, pipeline_question
            )
            widening_unclear_phrase, widening_chosen_value = (
                _resumed_widening_context(
                    pending, body.question, pipeline_question
                )
            )
            run_question = (
                pending["payload"].get("original_question", pipeline_question)
                if catalog_filters and pending is not None
                else pipeline_question
            )
            state = await asyncio.wait_for(
                _run_chat_pipeline(
                    run_question,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    asked_items=asked_items,
                    catalog_filters=catalog_filters,
                    cache_question=pipeline_question if catalog_filters else None,
                    widening_unclear_phrase=widening_unclear_phrase,
                    widening_chosen_value=widening_chosen_value,
                ),
                timeout=PIPELINE_TIMEOUT_SECONDS,
            )
            for node in PIPELINE_ORDER:
                if node not in started:
                    queue.put_nowait({"type": "node_skipped", "node": node})
            result = _build_response(
                state,
                request_id,
                conversation_id,
            ).model_dump()
        except asyncio.TimeoutError:
            logger.error("request_id=%s stream timed out", request_id)
            result = _build_error_response(request_id, TIMEOUT_ERROR)
        except Exception:
            logger.exception("request_id=%s unhandled stream error", request_id)
            result = _build_error_response(request_id, GENERIC_ERROR)
        queue.put_nowait({"type": "result", **result})
        queue.put_nowait(None)  # end of stream

    async def event_stream():
        task = asyncio.create_task(runner())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield json.dumps(event, default=str) + "\n"
        finally:
            # Client closed the tab or pressed Stop: stop paying for LLM calls.
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )