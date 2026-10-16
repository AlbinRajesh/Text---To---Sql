"""Master Golden Benchmark Runner (Catalogs 3, 4, and 5 mixed evaluation suite).

Features:
- SQL Evaluation: Tier 1 Canonical AST Equivalence -> Tier 2 Set-Based Execution Accuracy.
- Classifier/routing and multi-turn conversation cases from catalogs 3 and 4.
- Quota Management: Delay throttle between calls to prevent 429 rate limits.
- Micro-Batching: Run specific cases via ``--cases 1,5,12`` to conserve quota during coding.
- Time-Travel Checkpointing: Saves node states to JSON so failed cases can be replayed
  directly at Node 6 (saving 3 API calls per query).

Run from ``backend`` directory with:
    python -m app.tests.master_golden_benchmark
    python -m app.tests.master_golden_benchmark --cases 26,32,38 --delay 3
    python -m app.tests.master_golden_benchmark --resume-node6 32

Each run writes the accuracy CSV and a companion ``*_latency.csv`` containing
per-case stage, LLM/retry, database, and percentile timing records. The latency
report measures this benchmark runner, not the frontend HTTP/browser path.
"""

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from app.agent.nodes.business_rules import find_relevant_rules
from app.agent.nodes.classifier_rewrite import classify_and_rewrite
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    UnanswerableQuestionError,
    generate_query,
    preflight_refusals,
    repair_query_after_safety_rejection,
)
from app.agent.nodes.grade_sample import (
    GradeSampleError,
    MissingRelationshipError,
    SchemaGraderUnavailableError,
    grade_schema_sufficiency,
)
from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.run_self_fix import run_query
from app.agent.nodes.safety_check import (
    REPAIRABLE_SAFETY_RULES,
    SafetyViolationError,
    run_safety_check,
)
from app.agent.graph import (
    _pick_tie_column,
    build_tie_expansion_query,
    route_after_classify,
    run_pipeline,
)
from app.agent.nodes.schema_grounding import get_cached_schema, refresh_schema_cache
from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
    fetch_all,
    fetch_all_readonly,
)
from app.services.embedding_service import build_schema_index
from app.services.llm_service import LLMConfigError, LLMServiceError
from app.services.timing import start_request, timing_scope
from app.services.value_index import build_value_index

from app.tests.eval_engine import (
    evaluate_canonical_ast,
    evaluate_execution_accuracy,
    evaluate_refusal_or_block,
)
from app.tests.golden_benchmark_catalog_3 import (
    GOLDEN_BENCHMARK_CASES as GOLDEN_BENCHMARK_CASES_3,
)
from app.tests.golden_benchmark_catalog_4 import (
    GOLDEN_BENCHMARK_CASES as GOLDEN_BENCHMARK_CASES_4,
)
from app.tests.golden_benchmark_catalog_5 import (
    GOLDEN_BENCHMARK_CASES as GOLDEN_BENCHMARK_CASES_5,
)
from app.tests.query_generation_benchmark import _execution_columns

logger = logging.getLogger("master_golden_benchmark")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

GOLDEN_BENCHMARK_CASES = (
    GOLDEN_BENCHMARK_CASES_3
    + GOLDEN_BENCHMARK_CASES_4
    + GOLDEN_BENCHMARK_CASES_5
)

CSV_FIELDS = [
    "case_number",
    "category",
    "question",
    "expected_outcome",
    "evaluation_tier",
    "result_label",
    "passed",
    "failed_stage",
    "total_ms",
    "compiled_sql",
    "compiled_params",
    "golden_sql",
    "ast_match",
    "generated_rows_count",
    "golden_rows_count",
    "truncated",
    "detail_message",
    "error_type",
    "error_message",
    "note",
]
LATENCY_CSV_FIELDS = [
    "run_id",
    "measurement_scope",
    "case_number",
    "case_category",
    "case_status",
    "event_type",
    "stage",
    "duration_ms",
    "p50_ms",
    "p95_ms",
    "sample_count",
    "llm_role",
    "provider",
    "model",
    "attempts",
    "retry_wait_ms",
    "retry_causes",
    "success",
    "database_operation",
    "pool_wait_ms",
    "query_fetch_ms",
    "row_count",
    "notes",
]
REFUSAL_ERRORS = (
    UnanswerableQuestionError,
    MissingRelationshipError,
    SafetyViolationError,
    GradeSampleError,
)
# Errors caused by the environment (network, timeout), not by the model's answer.
INFRA_ERRORS = (
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
    SchemaGraderUnavailableError,
    LLMServiceError,
    LLMConfigError,
)
MAX_CONSECUTIVE_INFRA_ERRORS = 3


def _exception_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _is_infrastructure_error(exc: BaseException) -> bool:
    return any(
        isinstance(cause, INFRA_ERRORS)
        or type(cause).__name__ == "RateLimitError"
        for cause in _exception_chain(exc)
    )

def _prepare_golden_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    return cleaned.replace("%", "%%")


def _save_checkpoint(case_number: int, stage_name: str, payload: Dict[str, Any], checkpoint_dir: Path) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    file_path = checkpoint_dir / f"case_{case_number}_{stage_name}.json"
    with file_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, default=str, indent=2)


def _load_checkpoint(case_number: int, stage_name: str, checkpoint_dir: Path) -> Optional[Dict[str, Any]]:
    file_path = checkpoint_dir / f"case_{case_number}_{stage_name}.json"
    if not file_path.exists():
        return None
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _finish_case_timing(
    row: Dict[str, Any],
    started: float,
    timing_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    row["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    timing_events.append({
        "kind": "case_total",
        "seconds": row["total_ms"] / 1000,
        "case_status": row["evaluation_tier"],
    })
    row["_latency_events"] = timing_events
    return row


async def _run_single_case(
    case: Dict[str, Any],
    checkpoint_dir: Path,
    resume_node6: bool = False,
) -> Dict[str, Any]:
    started = time.perf_counter()
    timing_events = start_request()
    row: Dict[str, Any] = {
        "golden_rows_count": 0,
        "truncated": False,
        "detail_message": "",
        "case_number": case["number"],
        "category": case["category"],
        "question": case["question"],
        "expected_outcome": case["expected_outcome"],
        "evaluation_tier": "NOT_RUN",
        "result_label": "ERROR",
        "passed": "NO",
        "failed_stage": "none",
        "total_ms": 0.0,
        "compiled_sql": "",
        "compiled_params": "",
        "golden_sql": case.get("golden_sql", ""),
        "ast_match": "no",
        "generated_rows_count": 0,
        "golden_rows_count": 0,
        "detail_message": "",
        "error_type": "",
        "error_message": "",
        "note": case.get("note", ""),
    }

    try:
        case_type = case.get("case_type", "sql")
        if case_type == "classifier":
            row["failed_stage"] = "classify"
            with timing_scope("classify"):
                classification = await classify_and_rewrite(case["question"])
            actual_intent = classification.get("intent")
            actual_complexity = classification.get("complexity")
            actual_route = route_after_classify({
                "classifier_result": classification,
            })
            expected_intent = case["expected_intent"]
            expected_complexity = case.get("expected_complexity")
            expected_route = case["expected_route"]
            passed = (
                actual_intent == expected_intent
                and (not expected_complexity or actual_complexity == expected_complexity)
                and actual_route == expected_route
            )
            row["evaluation_tier"] = "TIER_1_CLASSIFIER_ROUTING"
            row["result_label"] = "CLASSIFIER_ROUTE_MATCH" if passed else "CLASSIFIER_ROUTE_DIFF"
            row["passed"] = "YES" if passed else "NO"
            row["detail_message"] = (
                f"expected intent={expected_intent}, complexity={expected_complexity or '*'}, "
                f"route={expected_route}; actual intent={actual_intent}, "
                f"complexity={actual_complexity}, route={actual_route}"
            )
            row["failed_stage"] = "eval_classifier"
            return _finish_case_timing(row, started, timing_events)

        if case_type == "conversation":
            conversation_ids: Dict[str, str] = {}
            turn_states = []
            for turn_index, turn in enumerate(case["turns"], 1):
                conversation_key = turn.get("conversation", "primary")
                conversation_id = conversation_ids.setdefault(
                    conversation_key, str(uuid.uuid4())
                )
                row["failed_stage"] = f"conversation_turn_{turn_index}"
                state = await run_pipeline(
                    turn["question"],
                    request_id=f"benchmark-{case['number']}-{turn_index}",
                    conversation_id=conversation_id,
                )
                turn_states.append(state)
                if turn.get("expected") == "answer" and (
                    state.get("escalated")
                    or state.get("answerable") is False
                    or not (state.get("execution_result") or state.get("cache_hit"))
                ):
                    raise RuntimeError(
                        f"Conversation setup turn {turn_index} did not produce "
                        "a successful data answer."
                    )
            final_state = turn_states[-1]
            for state in turn_states:
                timing_events.extend(state.get("timings", []))
            row["compiled_sql"] = (
                final_state.get("safety_check_result", {}).get("sql", "")
            )

            if case.get("expected_clarification"):
                final_answer = final_state.get("final_answer") or {}
                clarification_requested = bool(
                    final_state.get("clarification_needed")
                    or final_answer.get("clarification_needed")
                    or final_answer.get("answer_type") == "clarification"
                )
                passed = clarification_requested and not final_state.get("execution_result")
                row["evaluation_tier"] = "TIER_2_CONVERSATION"
                row["result_label"] = (
                    "CLARIFICATION_REQUESTED" if passed else "CLARIFICATION_MISSING"
                )
                row["passed"] = "YES" if passed else "NO"
                row["detail_message"] = (
                    "Ambiguous or context-free follow-up was clarified."
                    if passed
                    else "Expected an explicit clarification request; "
                    "the final turn did not expose one."
                )
                row["failed_stage"] = "eval_conversation"
                return _finish_case_timing(row, started, timing_events)

            if case["expected_outcome"] in {"refusal", "safe_block"}:
                refused_without_execution = (
                    not final_state.get("execution_result")
                    and (
                        final_state.get("escalated")
                        or final_state.get("answerable") is False
                    )
                )
                label, passed, detail = evaluate_refusal_or_block(
                    case["expected_outcome"],
                    "refused" if refused_without_execution else "sql_executed",
                    "conversation_guard",
                )
                row["evaluation_tier"] = "TIER_2_CONVERSATION"
                row["result_label"] = label
                row["passed"] = "YES" if passed else "NO"
                row["detail_message"] = detail
                row["failed_stage"] = "eval_conversation"
                return _finish_case_timing(row, started, timing_events)

            final_answer = final_state.get("final_answer") or {}
            if final_state.get("escalated") or final_state.get("answerable") is False:
                raise RuntimeError(
                    "Final follow-up did not produce an answer: "
                    f"{final_answer.get('error') or final_state.get('unanswerable_reasoning')}"
                )

            row["failed_stage"] = "execute_golden_sql"
            prepared_golden = _prepare_golden_sql(case["golden_sql"])
            with timing_scope("execute_golden_sql"):
                golden_rows = await fetch_all_readonly(prepared_golden)
            row["golden_rows_count"] = len(golden_rows)
            generated_rows = final_answer.get("results", [])
            row["generated_rows_count"] = len(generated_rows)
            row["truncated"] = bool(final_answer.get("truncated", False))
            row["failed_stage"] = "eval_conversation"
            label, passed, detail = evaluate_execution_accuracy(
                generated_rows, golden_rows, is_ordered_request=False
            )
            row["evaluation_tier"] = "TIER_2_CONVERSATION"
            row["result_label"] = label
            row["passed"] = "YES" if passed else "NO"
            row["detail_message"] = (
                "Follow-up result matches the golden query. " + detail
            )
            return _finish_case_timing(row, started, timing_events)

        # 1. State Resume or Sequential Pipeline Execution
        if resume_node6:
            with timing_scope("checkpoint_load"):
                checkpoint = _load_checkpoint(
                    case["number"], "node6_input", checkpoint_dir
                )
            if not checkpoint:
                raise RuntimeError(f"No checkpoint found to resume case {case['number']} at Node 6.")
            question = checkpoint["question"]
            grounded_schema = checkpoint["schema"]
            rules = checkpoint["rules"]
            relationships = checkpoint["relationships"]
            selected_tables = checkpoint["selected_tables"]
            row["failed_stage"] = "generate_query_resumed"
        else:
            # Node 1: Classifier
            row["failed_stage"] = "classify"
            with timing_scope("classify"):
                classification = await classify_and_rewrite(case["question"])
            if (
                classification.get("intent") != "data"
                and case["expected_outcome"] in {"refusal", "safe_block"}
            ):
                label, passed, detail = evaluate_refusal_or_block(
                    case["expected_outcome"], "refused", "classifier_chat_route"
                )
                row["evaluation_tier"] = "VALID_REFUSAL_RATE"
                row["result_label"] = label
                row["passed"] = "YES" if passed else "NO"
                row["detail_message"] = detail
                row["failed_stage"] = "classifier_refusal"
                return _finish_case_timing(row, started, timing_events)
            if classification.get("intent") != "data" and case["expected_outcome"] == "sql":
                raise RuntimeError(f"Intent classified as non-data: {classification.get('intent')}")
            question = classification.get("rewritten_question", case["question"])

            # Preflight safety
            row["failed_stage"] = "preflight"
            with timing_scope("preflight"):
                preflight_refusals(question, case["question"])

            # Node 3 & 4: Schema Grounding & Grade
            row["failed_stage"] = "schema_grade"
            with timing_scope("schema_grade"):
                grounded = await grade_schema_sufficiency(question)
            selected_tables = list(grounded["schema"])
            grounded_schema = grounded["schema"]
            relationships = grounded["relationships"]

            # Node 5: Business Rules
            row["failed_stage"] = "business_rules"
            with timing_scope("business_rules"):
                rules = find_relevant_rules(question)

            # Save state checkpoint for time-travel debugging
            with timing_scope("checkpoint_save"):
                _save_checkpoint(case["number"], "node6_input", {
                    "question": question,
                    "raw_question": case["question"],
                    "schema": grounded_schema,
                    "relationships": relationships,
                    "selected_tables": selected_tables,
                    "rules": rules,
                }, checkpoint_dir)

        # Node 6: Query Generation
        row["failed_stage"] = "generate_query"
        with timing_scope("generate_query"):
            query_plan = await generate_query(
                question,
                grounded_schema,
                rules,
                relationships,
                case["question"],
            )

        # Node 7: Safety Check & AST Guard
        row["failed_stage"] = "safety_check"
        with timing_scope("safety_check"):
            try:
                safe_query = run_safety_check(
                    query_plan,
                    valid_tables=selected_tables,
                    schema_context=grounded_schema,
                    relationships=relationships,
                )
            except SafetyViolationError as first_error:
                # Mirror graph.py: one repair attempt, repairable rules only.
                if first_error.rule_id not in REPAIRABLE_SAFETY_RULES:
                    raise
                query_plan = await repair_query_after_safety_rejection(
                    query_plan,
                    str(first_error),
                    question,
                    grounded_schema,
                    rules,
                    relationships,
                    case["question"],
                )
                safe_query = run_safety_check(
                    query_plan,
                    valid_tables=selected_tables,
                    schema_context=grounded_schema,
                    relationships=relationships,
                )
        row["compiled_sql"] = safe_query["sql"]
        row["compiled_params"] = json.dumps(safe_query["params"], default=str)
        logger.info(
            "Case #%s compiled SQL bind parameters: %s",
            case["number"],
            row["compiled_params"],
        )
        # If question was supposed to be a refusal/block but produced SQL:
        if case["expected_outcome"] in {"refusal", "safe_block"}:
            label, passed, detail = evaluate_refusal_or_block(
                case["expected_outcome"], "sql_executed", ""
            )
            row["result_label"] = label
            row["passed"] = "YES" if passed else "NO"
            row["detail_message"] = detail
            row["failed_stage"] = "safety_leak"
            return _finish_case_timing(row, started, timing_events)

        # =====================================================================
        # TIER 1 EVALUATION: CANONICAL AST EQUIVALENCE (0 DB Cost)
        # =====================================================================
        row["failed_stage"] = "eval_ast"
        with timing_scope("eval_ast"):
            ast_matched, ast_detail = evaluate_canonical_ast(
                safe_query["sql"], case["golden_sql"]
            )
        if ast_matched:
            row["evaluation_tier"] = "TIER_1_CANONICAL_AST"
            row["result_label"] = "AST_EXACT_MATCH"
            row["passed"] = "YES"
            row["ast_match"] = "yes"
            row["detail_message"] = "Queries are mathematically equivalent at the AST level."
            return _finish_case_timing(row, started, timing_events)

        # =====================================================================
        # TIER 2 EVALUATION: SET-BASED EXECUTION ACCURACY ON MYSQL
        # =====================================================================
        row["failed_stage"] = "execute_mysql"
        # Run generated SQL
        execution_columns = _execution_columns(query_plan, grounded_schema)
        tie_column = _pick_tie_column(query_plan, execution_columns)
        tie_expansion_safe_query = (
            build_tie_expansion_query(
                query_plan,
                selected_tables,
                grounded_schema,
                relationships,
            )
            if tie_column is not None
            else None
        )
        with timing_scope("execute_generated_sql"):
            gen_exec = await run_query({
                **safe_query,
                "columns": execution_columns,
                "tie_column": tie_column,
                "tie_expansion_query": (
                    {
                        "sql": tie_expansion_safe_query["sql"],
                        "params": tuple(tie_expansion_safe_query["params"]),
                        "row_limit": tie_expansion_safe_query["row_limit"],
                    }
                    if tie_expansion_safe_query is not None
                    else None
                ),
            })
        generated_rows = gen_exec.get("results", [])
        row["generated_rows_count"] = len(generated_rows)
        row["truncated"] = bool(gen_exec.get("truncated", False))

        # Run golden SQL
        prepared_golden = _prepare_golden_sql(case["golden_sql"])
        with timing_scope("execute_golden_sql"):
            golden_rows = await fetch_all_readonly(prepared_golden)
        row["golden_rows_count"] = len(golden_rows)

        row["failed_stage"] = "eval_execution"
        is_ordered = bool(case.get("note") and "order" in case["note"].lower())
        with timing_scope("eval_execution"):
            label, passed, detail = evaluate_execution_accuracy(
                generated_rows, golden_rows, is_ordered_request=is_ordered
            )
        row["evaluation_tier"] = "TIER_2_EXECUTION_ACCURACY"
        row["result_label"] = label
        row["passed"] = "YES" if passed else "NO"
        row["detail_message"] = detail

    except Exception as exc:
        exc_name = type(exc).__name__
        row["error_type"] = exc_name
        row["error_message"] = str(exc)

        if _is_infrastructure_error(exc):
            row["evaluation_tier"] = "NOT_EVALUATED"
            row["result_label"] = "NOT_EVALUATED_INFRA"
            row["passed"] = ""
            row["detail_message"] = (
                f"Infrastructure failure at {row['failed_stage']}: "
                f"{exc_name} - {exc}"
            )
        elif (
            case["expected_outcome"] in {"refusal", "safe_block"}
            and isinstance(exc, REFUSAL_ERRORS)
        ):
            label, passed, detail = evaluate_refusal_or_block(
                case["expected_outcome"], "refused", exc_name
            )
            row["evaluation_tier"] = "VALID_REFUSAL_RATE"
            row["result_label"] = label
            row["passed"] = "YES" if passed else "NO"
            row["detail_message"] = detail
        else:
            row["result_label"] = "FAILED_EXCEPTION"
            row["passed"] = "NO"
            row["detail_message"] = f"Failed at {row['failed_stage']}: {exc_name} - {exc}"

    return _finish_case_timing(row, started, timing_events)


def _latency_report_rows(
    rows: List[Dict[str, Any]],
    run_id: str,
    setup_ms: float,
    throttle_wait_ms: float,
) -> List[Dict[str, Any]]:
    report_rows = [
        {
            "run_id": run_id,
            "measurement_scope": "master_benchmark",
            "case_number": "",
            "case_category": "",
            "case_status": "",
            "event_type": "run_setup",
            "stage": "connect_refresh_schema_index",
            "duration_ms": round(setup_ms, 2),
            "notes": "Benchmark initialization; not per-question frontend latency.",
        },
        {
            "run_id": run_id,
            "measurement_scope": "master_benchmark",
            "case_number": "",
            "case_category": "",
            "case_status": "",
            "event_type": "benchmark_pacing",
            "stage": "inter_case_throttle",
            "duration_ms": round(throttle_wait_ms, 2),
            "notes": "Intentional delay between cases to limit provider rate limits.",
        },
    ]
    stage_samples: Dict[str, List[float]] = {}
    for case_row in rows:
        case_number = case_row.get("case_number", "")
        case_status = case_row.get("evaluation_tier", "")
        events = case_row.get("_latency_events", [])
        if not events and case_status == "NOT_RUN":
            report_rows.append({
                "run_id": run_id,
                "measurement_scope": "master_benchmark",
                "case_number": case_number,
                "case_category": case_row.get("category", ""),
                "case_status": case_status,
                "event_type": "case_not_run",
                "stage": case_row.get("failed_stage", "not_run"),
                "duration_ms": 0,
                "notes": case_row.get("detail_message", ""),
            })
        for event in events:
            kind = event.get("kind")
            output = {
                "run_id": run_id,
                "measurement_scope": "master_benchmark",
                "case_number": case_number,
                "case_category": case_row.get("category", ""),
                "case_status": event.get("case_status", case_status),
            }
            if kind == "node":
                stage = event.get("name", "")
                duration_ms = round(event.get("seconds", 0) * 1000, 2)
                stage_samples.setdefault(stage, []).append(duration_ms)
                output.update({
                    "event_type": "stage",
                    "stage": stage,
                    "duration_ms": duration_ms,
                })
            elif kind == "case_total":
                duration_ms = round(event.get("seconds", 0) * 1000, 2)
                stage_samples.setdefault(
                    "benchmark_case_end_to_end", []
                ).append(duration_ms)
                output.update({
                    "event_type": "case_total",
                    "stage": "benchmark_case_end_to_end",
                    "duration_ms": duration_ms,
                    "notes": (
                        "Includes benchmark evaluation; excludes inter-case pacing "
                        "and frontend rendering."
                    ),
                })
            elif kind == "llm":
                output.update({
                    "event_type": "llm_call",
                    "stage": event.get("node", ""),
                    "duration_ms": round(event.get("seconds", 0) * 1000, 2),
                    "llm_role": event.get("role", ""),
                    "provider": event.get("provider", ""),
                    "model": event.get("model", ""),
                    "attempts": event.get("attempts", ""),
                    "retry_wait_ms": round(
                        event.get("backoff_seconds", 0) * 1000, 2
                    ),
                    "retry_causes": ",".join(event.get("causes", [])),
                    "success": event.get("ok", ""),
                })
            elif kind == "database":
                output.update({
                    "event_type": "database_call",
                    "stage": event.get("stage", ""),
                    "duration_ms": round(
                        (
                            event.get("pool_wait_seconds", 0)
                            + event.get("query_fetch_seconds", 0)
                        ) * 1000,
                        2,
                    ),
                    "database_operation": event.get("operation", ""),
                    "pool_wait_ms": round(
                        event.get("pool_wait_seconds", 0) * 1000, 2
                    ),
                    "query_fetch_ms": round(
                        event.get("query_fetch_seconds", 0) * 1000, 2
                    ),
                    "row_count": (
                        event.get("row_count")
                        if event.get("row_count") is not None
                        else ""
                    ),
                    "success": event.get("ok", ""),
                })
            else:
                continue
            report_rows.append(output)
    for stage, samples in sorted(stage_samples.items()):
        ordered = sorted(samples)
        p50 = ordered[max(math.ceil(0.50 * len(ordered)) - 1, 0)]
        p95 = ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]
        report_rows.append({
            "run_id": run_id,
            "measurement_scope": "master_benchmark",
            "case_number": "",
            "case_category": "",
            "case_status": "",
            "event_type": "latency_summary",
            "stage": stage,
            "p50_ms": p50,
            "p95_ms": p95,
            "sample_count": len(samples),
            "notes": "Nearest-rank percentile across measured cases in this run.",
        })
    report_rows.append({
        "run_id": run_id,
        "measurement_scope": "master_benchmark",
        "case_number": "",
        "case_category": "",
        "case_status": "",
        "event_type": "scope_note",
        "stage": "",
        "duration_ms": "",
        "notes": (
            "This runner does not execute the frontend HTTP route, semantic cache "
            "lookup, result grader, answer rendering, or browser/network path. "
            "Use the report to compare measured benchmark stages, not as full "
            "frontend response latency."
        ),
    })
    return report_rows


def _write_latency_report(
    path: Path,
    rows: List[Dict[str, Any]],
    run_id: str,
    setup_ms: float,
    throttle_wait_ms: float,
) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=LATENCY_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(
            _latency_report_rows(rows, run_id, setup_ms, throttle_wait_ms)
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Master Golden Benchmark Runner")
    parser.add_argument("--cases", type=str, help="Comma-separated case numbers to run (e.g. 26,32,38)")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds to delay between cases to prevent 429 limits")
    parser.add_argument("--output", type=str, default="", help="Custom output CSV path")
    parser.add_argument("--resume-node6", type=int, help="Resume a specific case directly at Node 6 using checkpoint")
    args = parser.parse_args()

    # Determine cases to run
    selected_cases = GOLDEN_BENCHMARK_CASES
    if args.resume_node6:
        selected_cases = [c for c in GOLDEN_BENCHMARK_CASES if c["number"] == args.resume_node6]
        if not selected_cases:
            logger.error("No case found matching number %d", args.resume_node6)
            sys.exit(1)
    elif args.cases:
        case_nums = {int(x.strip()) for x in args.cases.split(",") if x.strip().isdigit()}
        selected_cases = [c for c in GOLDEN_BENCHMARK_CASES if c["number"] in case_nums]

    # Output path
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(args.output) if args.output else Path("app/tests") / f"daily_golden_benchmark_{timestamp}.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    latency_output_path = output_path.with_name(
        f"{output_path.stem}_latency.csv"
    )
    checkpoint_dir = Path("app/tests/checkpoints")

    logger.info("Initializing database connections and schema cache...")
    setup_started = time.perf_counter()
    await connect_to_mysql()
    await connect_to_read_replica()
    try:
        await refresh_schema_cache()
        await build_schema_index(get_cached_schema())
        await build_value_index()
        setup_ms = (time.perf_counter() - setup_started) * 1000

        logger.info("Starting benchmark run on %d cases...", len(selected_cases))
        rows = []
        pass_count = 0
        evaluated_count = 0
        consecutive_infra_errors = 0
        throttle_wait_ms = 0.0

        for idx, case in enumerate(selected_cases, 1):
            logger.info("[%d/%d] Running Case #%d (%s): %s", idx, len(selected_cases), case["number"], case["category"], case["question"])
            row = await _run_single_case(
                case,
                checkpoint_dir=checkpoint_dir,
                resume_node6=bool(args.resume_node6),
            )
            rows.append(row)
            if row["evaluation_tier"] == "NOT_EVALUATED":
                consecutive_infra_errors += 1
                logger.warning(
                    "   -> NOT EVALUATED (infrastructure failure; %d consecutive)",
                    consecutive_infra_errors,
                )
            else:
                consecutive_infra_errors = 0
                evaluated_count += 1
            if row["passed"] == "YES":
                pass_count += 1
                logger.info("   -> PASS [%s] (Tier: %s)", row["result_label"], row["evaluation_tier"])
            elif row["evaluation_tier"] != "NOT_EVALUATED":
                logger.warning("   -> FAIL [%s] at stage '%s': %s", row["result_label"], row["failed_stage"], row["detail_message"])

            if consecutive_infra_errors >= MAX_CONSECUTIVE_INFRA_ERRORS:
                logger.error(
                    "Stopping benchmark after %d consecutive infrastructure failures.",
                    consecutive_infra_errors,
                )
                for skipped_case in selected_cases[idx:]:
                    rows.append({
                        "case_number": skipped_case["number"],
                        "category": skipped_case["category"],
                        "question": skipped_case["question"],
                        "expected_outcome": skipped_case["expected_outcome"],
                        "evaluation_tier": "NOT_RUN",
                        "result_label": "NOT_RUN",
                        "passed": "",
                        "failed_stage": "not_run",
                        "total_ms": 0.0,
                        "compiled_sql": "",
                        "compiled_params": "",
                        "golden_sql": skipped_case.get("golden_sql", ""),
                        "ast_match": "no",
                        "generated_rows_count": 0,
                        "golden_rows_count": 0,
                        "truncated": False,
                        "detail_message": (
                            "Not run because the benchmark stopped after "
                            "consecutive infrastructure failures."
                        ),
                        "error_type": "",
                        "error_message": "",
                        "note": skipped_case.get("note", ""),
                    })
                break

            # Throttle delay to protect free API limits
            if idx < len(selected_cases) and args.delay > 0:
                throttle_started = time.perf_counter()
                await asyncio.sleep(args.delay)
                throttle_wait_ms += (
                    time.perf_counter() - throttle_started
                ) * 1000

        # Write CSV report
        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows({
                key: value for key, value in row.items()
                if key != "_latency_events"
            } for row in rows)

        _write_latency_report(
            latency_output_path,
            rows,
            timestamp,
            setup_ms,
            throttle_wait_ms,
        )

        pass_rate = round((pass_count / evaluated_count) * 100, 1) if evaluated_count else 0.0
        logger.info("=" * 60)
        not_evaluated_count = sum(
            row["evaluation_tier"] == "NOT_EVALUATED" for row in rows
        )
        not_run_count = sum(row["evaluation_tier"] == "NOT_RUN" for row in rows)
        logger.info(
            "BENCHMARK COMPLETED: %d/%d evaluated passed (%.1f%%); "
            "%d not evaluated; %d not run",
            pass_count,
            evaluated_count,
            pass_rate,
            not_evaluated_count,
            not_run_count,
        )
        logger.info("Detailed report saved to: %s", output_path)
        logger.info("Latency report saved to: %s", latency_output_path)
        logger.info("=" * 60)

    finally:
        await close_mysql_connection()


if __name__ == "__main__":
    asyncio.run(main())
