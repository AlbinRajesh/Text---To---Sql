"""
return_save.py  (Node 10: Return + Save)

Final "happy path" node. Builds the final_answer shape the user actually
sees, and — only for answers that are safe to reuse — saves the question
into the semantic cache (cache_check.py's store()) for future near-duplicate
questions.

Cases:
  0. Casual chat — reply only.
  1. Cache hit (Node 2) — reuse the cached rows; rebuild the summary for
     the current question rather than storing generated prose in the cache.
  2. Confirmed unanswerable (Node 6) — safe to cache.
  3. Executed + Node-9-graded-valid result — safe to cache.

Answer shape for real results:
  results, columns, count, row_count -> stable row data and returned-row count
  answer_type              -> "single_value" | "table" | "empty"
  column_labels            -> {column_name: "Friendly label"}
  single_value             -> the value, only when answer_type is "single_value"
  truncated, row_limit     -> exact probe result and configured display cap
  summary                  -> one verified sentence (only present if it passed checks)

The summary is NEVER stored in the cache.
"""

import logging
from typing import Any, Dict, List

from app.agent.nodes.cache_check import store as cache_store
from app.services.summary_service import generate_summary

logger = logging.getLogger(__name__)

# Must match the LIMIT values applied in safety_check.py.
UNGROUPED_ROW_LIMIT = 200
GROUPED_ROW_LIMIT = 1000


def _column_name(column: Any) -> str:
    """Columns may arrive as plain strings or as dicts; return the name either way."""
    if isinstance(column, dict):
        return str(column.get("name") or column.get("column") or "")
    return str(column)


def _humanize(name: str) -> str:
    """'shop_count' -> 'Shop count', 'category_id' -> 'Category ID'."""
    words = [w for w in name.replace("_", " ").split() if w]
    if not words:
        return name
    words = ["ID" if w.lower() == "id" else w for w in words]
    label = " ".join(words)
    return label[:1].upper() + label[1:]


def _single_value(results: List[Any]) -> tuple[bool, Any]:
    """Return the one cell when there is exactly one row and one column."""
    if len(results) != 1:
        return False, None
    row = results[0]
    if isinstance(row, dict):
        return (True, next(iter(row.values()))) if len(row) == 1 else (False, None)
    if isinstance(row, (list, tuple)):
        return (True, row[0]) if len(row) == 1 else (False, None)
    return False, None


def _build_result_answer(
    execution_result: Dict[str, Any], generated_query: Dict[str, Any]
) -> Dict[str, Any]:
    """Builds the structured answer for a real executed result."""
    results = execution_result.get("results", []) or []
    columns = execution_result.get("columns", []) or []
    count = execution_result.get("row_count", execution_result.get("count", len(results)))

    column_names = [_column_name(c) for c in columns]
    column_labels = {name: _humanize(name) for name in column_names if name}

    limit = execution_result.get(
        "row_limit",
        GROUPED_ROW_LIMIT if generated_query.get("group_by") else UNGROUPED_ROW_LIMIT,
    )
    truncated = bool(execution_result.get("truncated", False))

    answer: Dict[str, Any] = {
        "results": results,
        "columns": columns,
        "count": count,
        "row_count": count,
        "column_formats": execution_result.get("column_formats", {}),
        "suggestions": execution_result.get("suggestions", []),
        "column_labels": column_labels,
        "truncated": truncated,
        "row_limit": limit,
    }

    no_data_reason = execution_result.get("no_data_reason")
    if no_data_reason is not None:
        if no_data_reason == "no_rows":
            answer["message"] = "No data for this filter."
        elif no_data_reason == "all_null":
            aggregate_functions = {
                item.get("aggregate")
                for item in generated_query.get("select_items", [])
                if isinstance(item, dict)
            }
            if aggregate_functions == {"AVG"}:
                answer["message"] = (
                    "The average is unavailable because the matching records "
                    "have no amount values."
                )
            elif aggregate_functions == {"SUM"}:
                answer["message"] = (
                    "The sum is unavailable because the matching records "
                    "have no amount values."
                )
            else:
                answer["message"] = (
                    "The aggregate is unavailable because the matching "
                    "records have no non-null values."
                )
        else:
            raise ValueError(f"Unsupported no_data_reason: {no_data_reason!r}")
        answer["no_data_reason"] = no_data_reason
        answer["matching_row_count"] = execution_result.get("matching_row_count")
        results = []
        answer["results"] = results
        answer["count"] = 0
        answer["row_count"] = 0

    if execution_result.get("tie_at_limit") and results:
        answer["message"] = (
            "Other results share this same value, so this is not a unique answer."
        )
        answer["tie_at_limit"] = True

    if not results:
        answer["answer_type"] = "empty"
        answer["truncated"] = False
        answer.setdefault("message", "No data found for this request.")
        return answer

    has_single, single = _single_value(results)
    if has_single:
        answer["answer_type"] = "single_value"
        answer["single_value"] = single
        answer["truncated"] = False
    else:
        answer["answer_type"] = "table"
    return answer


async def _attach_summary(final_answer: Dict[str, Any], original_question: str) -> None:
    """Adds a verified summary sentence if one can be made. Never raises."""
    final_answer.pop("summary", None)
    summary = await generate_summary(original_question, final_answer)
    if summary:
        final_answer["summary"] = summary


async def build_and_save_answer(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry point for Node 10.

    Returns:
        {"final_answer": dict, "saved_to_cache": bool}
        Caller (graph.py) writes these back into state.
    """
    classifier = state["classifier_result"]
    question = classifier["rewritten_question"]
    original_question = classifier.get("original_question") or question

    # --- Case 0: casual chat. Reply only; nothing to run, nothing to cache ---
    if classifier.get("intent") == "chat":
        final_answer = {
            "chat_reply": classifier.get("chat_reply")
            or "Hi! I can answer questions about your business data."
        }
        logger.info("Node 10: casual chat reply for %r", question)
        return {"final_answer": final_answer, "saved_to_cache": False}

    if classifier.get("intent") == "clarification":
        final_answer = {
            "clarification_question": classifier.get("clarification_question"),
            "clarification_options": classifier.get("clarification_options", []),
        }
        if classifier.get("clarification_kind"):
            final_answer["clarification_kind"] = classifier["clarification_kind"]
        if classifier.get("clarification_option_records"):
            final_answer["clarification_option_records"] = classifier[
                "clarification_option_records"
            ]
        if "confirmation_complete" in classifier:
            final_answer["confirmation_complete"] = (
                classifier["confirmation_complete"] is True
            )
        if "clarification_topic" in classifier:
            final_answer["clarification_topic"] = classifier[
                "clarification_topic"
            ]
        return {"final_answer": final_answer, "saved_to_cache": False}

    # --- Case 1: cache hit — reuse rows, rebuild the summary for this user ---
    if state.get("cache_hit"):
        final_answer = dict(state.get("cached_answer") or {})  # copy: never edit the cached object
        await _attach_summary(final_answer, original_question)
        logger.info("Node 10: returning cached answer for %r", question)
        return {"final_answer": final_answer, "saved_to_cache": False}

    # --- Case 2: confirmed unanswerable ---
    if not state.get("answerable", True):
        final_answer = {
            "answer": None,
            "reasoning": state.get("unanswerable_reasoning"),
        }
        logger.info("Node 10: not caching unanswerable verdict for %r", question)
        return {"final_answer": final_answer, "saved_to_cache": False}

    # --- Case 3: real executed result ---
    final_answer = _build_result_answer(
        state.get("execution_result", {}) or {},
        state.get("generated_query", {}) or {},
    )

    # Defense in depth: only cache a result Node 9 graded valid and nothing escalated.
        # Defense in depth: only cache a result Node 9 graded valid and nothing escalated.
    verdict = state.get("result_grader_verdict") or {}
    if verdict.get("is_valid") and not state.get("escalated"):
        if classifier.get("unresolved_catalog_items"):
            logger.info("partial_answer_cache_store_bypass=1")
            saved = False
        elif final_answer.get("answer_type") == "empty":
            # 0 rows depends on the data at this moment and on how the question
            # was understood. Caching it would repeat a possibly wrong
            # "no records" answer for up to CACHE_TTL_SECONDS.
            logger.info("Node 10: not caching empty result for %r", question)
            saved = False
        else:
            cache_question = classifier.get("cache_question", question)
            catalog_filters = classifier.get("catalog_filters", [])
            catalog_filter_combiner = classifier.get("catalog_filter_combiner")
            if catalog_filters:
                await cache_store(
                    cache_question,
                    dict(final_answer),
                    state.get("context_version", "default"),
                    catalog_filters=catalog_filters,
                    catalog_filter_combiner=catalog_filter_combiner,
                )
            else:
                await cache_store(
                    cache_question,
                    dict(final_answer),
                    state.get("context_version", "default"),
                )
            logger.info("Node 10: cached graded-valid result for %r", question)
            saved = True
        await _attach_summary(final_answer, original_question)
    else:
        logger.warning(
            "Node 10: reached with an ungraded or invalid result for %r — "
            "NOT caching. This should not happen if graph.py routing is "
            "correct; check route_after_result_grader.",
            question,
        )
        saved = False

    return {"final_answer": final_answer, "saved_to_cache": saved}