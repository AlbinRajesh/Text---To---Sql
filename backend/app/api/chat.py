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
    consume_pending_interaction,
    store_pending_interaction,
)
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
            message=final_answer.get("message"),
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
    options: List[str],
) -> Optional[str]:
    normalized = re.sub(r"[^a-z0-9 ]", "", value.casefold()).strip()
    for option in options:
        candidate = re.sub(r"[^a-z0-9 ]", "", option.casefold()).strip()
        if normalized == candidate or (
            len(normalized) >= 3
            and (candidate.startswith(normalized) or normalized.startswith(candidate))
        ):
            return option
    return None


def _is_short_clarification_reply(value: str) -> bool:
    normalized = value.strip().casefold()
    return (
        0 < len(normalized) <= 40
        and "?" not in normalized
        and not re.match(
            r"^(show|list|how|what|which|can|could|please)\b",
            normalized,
        )
    )


def _clarification_requires_selection(value: str) -> bool:
    return bool(
        re.search(r"\b(?:which|what)\b|\bor\b", value, re.IGNORECASE)
    )


async def _handle_pending_or_sql(
    question: str,
    request_id: str,
    conversation_id: str,
) -> tuple[Optional[ChatResponse], str]:
    """Handle safe SQL confirmation and clarification continuation before classification."""
    pending = consume_pending_interaction(conversation_id)
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
            option = _matched_clarification_option(
                question,
                payload.get("options", []),
            )
            clarification_question = payload.get("clarification_question", "")
            if (
                _is_affirmative(question)
                and not option
                and _clarification_requires_selection(clarification_question)
            ):
                store_pending_interaction(
                    conversation_id,
                    "clarification",
                    payload,
                )
                return (
                    ChatResponse(
                        request_id=request_id,
                        conversation_id=conversation_id,
                        status="clarification",
                        message=clarification_question,
                        clarification_options=payload.get("options", []),
                    ),
                    question,
                )
            if option is None and _is_short_clarification_reply(question):
                option = question.strip()
            if _is_affirmative(question) or option is not None:
                clarification = (
                    clarification_question
                    if _is_affirmative(question)
                    else f"The user selected: {option}."
                )
                return (
                    None,
                    f"{payload['question']}\nClarification: {clarification}",
                )
            if _is_negative(question):
                return (
                    ChatResponse(
                        request_id=request_id,
                        conversation_id=conversation_id,
                        status="chat",
                        message="Okay. I won't continue that request.",
                    ),
                    question,
                )

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
        store_pending_interaction(
            conversation_id,
            "clarification",
            {
                "question": state.get("user_question", ""),
                "clarification_question": final_answer["clarification_question"],
                "options": final_answer.get("clarification_options", []),
            },
        )


async def _run_chat_pipeline(
    question: str,
    request_id: str,
    conversation_id: str,
) -> Dict[str, Any]:
    state = await run_pipeline(
        question,
        request_id=request_id,
        conversation_id=conversation_id,
    )
    _store_clarification_if_needed(state, conversation_id)
    return state


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest):
    request_id = str(uuid.uuid4())
    conversation_id = str(body.conversation_id or uuid.uuid4())
    await clear_suggestion_plans(conversation_id)

    try:
        direct_response, pipeline_question = await _handle_pending_or_sql(
            body.question,
            request_id,
            conversation_id,
        )
        if direct_response is not None:
            return direct_response
        state = await asyncio.wait_for(
            _run_chat_pipeline(
                pipeline_question,
                request_id=request_id,
                conversation_id=conversation_id,
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
            state = await asyncio.wait_for(
                _run_chat_pipeline(
                    pipeline_question,
                    request_id=request_id,
                    conversation_id=conversation_id,
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