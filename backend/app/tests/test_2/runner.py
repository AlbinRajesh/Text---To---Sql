from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiomysql
from sqlglot import exp, parse

from app.tests.e2e_frontend.utils.oracle import _fetch_oracle_rows_in_worker
from app.tests.test_2.api_client import ChatStreamError, post_chat_stream
from app.tests.test_2.evaluators import evaluate_case
from app.tests.test_2.reporting import (
    load_pipeline_diagnostics,
    write_case_trace,
    write_run_reports,
)
from app.services.errors import InfrastructureError

logger = logging.getLogger("test_2")

TEST_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = TEST_DIR.parents[3]
CASE_FILE = TEST_DIR / "case_main.json"

REQUIRED_CASE_FIELDS = {
    "id", "split", "question", "language", "category", "priority",
    "origin_context", "expected_outcome_type", "golden_sql",
    "expected_tables", "compare_rules", "expected_checkpoints",
    "must_not_rules", "multi_turn",
}
VALID_SPLITS = {"tuning", "exam"}
VALID_ORDERING_CONTRACTS = {"none", "ranking", "top_n", "latest"}
VALID_TIE_POLICIES = {
    "none",
    "all_optimal_rows",
    "cutoff_value_equivalence",
}


def _validate_read_only_select(sql: str) -> None:
    statements = [statement for statement in parse(sql, read="mysql") if statement]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("Golden SQL must be exactly one read-only SELECT query.")
    if any(
        isinstance(
            node,
            (
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Drop,
                exp.Create,
                exp.Alter,
                exp.Into,
            ),
        )
        for node in statements[0].walk()
    ):
        raise ValueError("Golden SQL contains a disallowed write operation.")


def validate_test_catalog(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark cases with invalid golden SQL before any API calls are made."""
    for case in cases:
        case_id = case.get("id")
        golden_sql = case.get("golden_sql")
        case.pop("_invalid_test_error", None)

        if not isinstance(golden_sql, str) or not golden_sql.strip():
            logger.warning("Case %s has no golden_sql.", case_id)
            continue

        try:
            _validate_read_only_select(golden_sql)
        except Exception as exc:
            logger.error("Invalid test case %s: broken golden SQL: %s", case_id, exc)
            case["_invalid_test_error"] = str(exc)
    return cases


def load_cases(path: Path = CASE_FILE) -> list[dict[str, Any]]:
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load case file {path}: {exc}") from exc
    if not isinstance(cases, list):
        raise ValueError("case_main.json must contain a JSON array.")

    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"Case at index {index} must be a JSON object.")
        missing = REQUIRED_CASE_FIELDS - case.keys()
        if missing:
            raise ValueError(f"Case at index {index} is missing fields: {sorted(missing)}")
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"Case at index {index} has an invalid id.")
        if case_id in seen_ids:
            raise ValueError(f"Duplicate case id: {case_id}")
        seen_ids.add(case_id)
        if case["split"] not in VALID_SPLITS:
            raise ValueError(
                f"Case {case_id} has invalid split {case['split']!r}; "
                f"expected one of {sorted(VALID_SPLITS)}."
            )
        if not isinstance(case["question"], str) or not case["question"].strip():
            raise ValueError(f"Case {case_id} must have a non-empty question.")
        if not isinstance(case["expected_tables"], list):
            raise ValueError(f"Case {case_id}: expected_tables must be an array.")
        if not isinstance(case["must_not_rules"], list):
            raise ValueError(f"Case {case_id}: must_not_rules must be an array.")
        if not isinstance(case["multi_turn"], dict):
            raise ValueError(f"Case {case_id}: multi_turn must be an object.")
        compare_rules = case["compare_rules"]
        if not isinstance(compare_rules, dict):
            raise ValueError(f"Case {case_id}: compare_rules must be an object.")
        ordering_contract = compare_rules.get("ordering_contract")
        if (
            ordering_contract is not None
            and ordering_contract not in VALID_ORDERING_CONTRACTS
        ):
            raise ValueError(
                f"Case {case_id}: invalid ordering_contract "
                f"{ordering_contract!r}."
            )
        tie_policy = compare_rules.get("tie_policy", "none")
        if tie_policy not in VALID_TIE_POLICIES:
            raise ValueError(
                f"Case {case_id}: invalid tie_policy {tie_policy!r}."
            )
        if tie_policy == "all_optimal_rows" and not isinstance(
            case.get("golden_sql"), str
        ):
            raise ValueError(
                f"Case {case_id}: all_optimal_rows requires a complete tie oracle in golden_sql."
            )
        if tie_policy == "cutoff_value_equivalence":
            if ordering_contract not in {"ranking", "top_n"}:
                raise ValueError(
                    f"Case {case_id}: cutoff_value_equivalence requires a "
                    "ranking or top_n ordering_contract."
                )
            if not isinstance(compare_rules.get("tie_metric_column"), str) or not (
                compare_rules["tie_metric_column"].strip()
            ):
                raise ValueError(
                    f"Case {case_id}: cutoff_value_equivalence requires "
                    "tie_metric_column."
                )
    return cases


def _questions_for_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    multi_turn = case.get("multi_turn") or {}
    turns = multi_turn.get("turns") or []
    if not multi_turn.get("is_multi_turn"):
        return [{"question": case["question"], "oracle_sql": case.get("golden_sql")}]
    if not isinstance(turns, list) or not turns:
        raise ValueError(
            f"Case {case['id']} is marked multi-turn but has no turns."
        )
    normalized = []
    for index, turn in enumerate(turns):
        if isinstance(turn, str):
            normalized.append({"question": turn})
        elif isinstance(turn, dict) and isinstance(turn.get("question"), str):
            normalized.append(turn)
        else:
            raise ValueError(
                f"Case {case['id']} turn {index} must be a question string or object."
            )
    return normalized


async def _execute_oracle(sql: str | None) -> list[dict[str, Any]] | None:
    if not sql:
        return None
    _validate_read_only_select(sql)
    return await asyncio.to_thread(_fetch_oracle_rows_in_worker, sql)


async def _run_case(
    case: dict[str, Any],
    backend_url: str,
    timeout: float,
    delay: float,
    trace_dir: Path,
    run_id: str,
    oracle_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    questions = _questions_for_case(case)
    conversation_id = str(uuid4()) if len(questions) > 1 else None
    captures: list[dict[str, Any]] = []
    for index, turn in enumerate(questions):
        if index and delay:
            await asyncio.sleep(delay)
        capture = await asyncio.to_thread(
            post_chat_stream,
            backend_url,
            turn["question"],
            conversation_id=conversation_id,
            timeout=timeout,
        )
        captures.append(capture)

    final_capture = captures[-1]
    result = final_capture["result"]
    pipeline_diagnostics = load_pipeline_diagnostics(
        final_capture.get("request_id"), REPOSITORY_ROOT
    )
    evaluation = evaluate_case(
        case,
        final_capture["events"],
        result,
        oracle_rows,
        sum(capture["latency_ms"] for capture in captures),
        pipeline_diagnostics=pipeline_diagnostics,
    )
    trace_path = write_case_trace(
        trace_dir,
        run_id,
        case,
        {
            **final_capture,
            "latency_ms": sum(capture["latency_ms"] for capture in captures),
            "expected_rows": oracle_rows,
            "turns": [
                {
                    "request_id": capture.get("request_id"),
                    "events": capture.get("events"),
                    "result": capture.get("result"),
                    "latency_ms": capture.get("latency_ms"),
                }
                for capture in captures
            ],
        },
        evaluation,
        REPOSITORY_ROOT,
    )
    schema_metrics = evaluation["checks"]["schema_efficiency"]
    return {
        "case_id": case["id"],
        "split": case["split"],
        "priority": case.get("priority"),
        "grading_bucket": evaluation["grading_bucket"],
        "safety_leak": evaluation["safety_leak"],
        "latency_ms": sum(capture["latency_ms"] for capture in captures),
        "schema_precision": schema_metrics["precision"],
        "schema_recall": schema_metrics["recall"],
        "checks": evaluation["checks"],
        "trace_path": str(trace_path),
        "error": "",
    }


async def run(args: argparse.Namespace) -> int:
    cases = validate_test_catalog(load_cases(Path(args.case_file)))
    selected = [case for case in cases if case["split"] == args.mode]
    if args.case_ids:
        requested_ids = set(args.case_ids.split(","))
        selected = [case for case in selected if case["id"] in requested_ids]
        missing = requested_ids - {case["id"] for case in selected}
        if missing:
            raise ValueError(
                f"Requested IDs are absent from the {args.mode!r} split: "
                f"{sorted(missing)}"
            )
    if not selected:
        raise ValueError(
            f"No cases are assigned to split {args.mode!r} in {args.case_file}."
        )

    run_id = f"{args.mode}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    backend_url = args.backend_url.rstrip("/")
    trace_dir = Path(args.trace_dir)
    output_dir = Path(args.output_dir)

    rows: list[dict[str, Any]] = []
    consecutive_errors = 0
    for index, case in enumerate(selected):
        logger.info(
            "[%d/%d] %s (%s): %s",
            index + 1, len(selected), case["id"], args.mode, case["question"],
        )
        started = time.perf_counter()
        oracle_rows: list[dict[str, Any]] | None = None
        invalid_test_error = case.get("_invalid_test_error")
        if invalid_test_error:
            capture = {
                "request_id": None,
                "events": [],
                "result": {
                    "status": "not_run",
                    "message": "Skipped because this test case has invalid golden SQL.",
                },
                "latency_ms": 0.0,
                "error": invalid_test_error,
            }
            evaluation = evaluate_case(
                case,
                [],
                capture["result"],
                None,
                0.0,
            )
            trace_path = write_case_trace(
                trace_dir,
                run_id,
                case,
                capture,
                evaluation,
                REPOSITORY_ROOT,
            )
            schema_metrics = evaluation["checks"]["schema_efficiency"]
            row = {
                "case_id": case["id"],
                "split": case["split"],
                "priority": case.get("priority"),
                "grading_bucket": evaluation["grading_bucket"],
                "safety_leak": False,
                "latency_ms": 0.0,
                "schema_precision": schema_metrics["precision"],
                "schema_recall": schema_metrics["recall"],
                "checks": evaluation["checks"],
                "trace_path": str(trace_path),
                "error": invalid_test_error,
            }
            rows.append(row)
            logger.error("Skipping invalid test %s: %s", case["id"], invalid_test_error)
            continue

        try:
            questions = _questions_for_case(case)
            oracle_sql = questions[-1].get("oracle_sql", case.get("golden_sql"))
            oracle_rows = await _execute_oracle(oracle_sql) if oracle_sql else None
            row = await _run_case(
                case,
                backend_url,
                args.timeout,
                0.0,
                trace_dir,
                run_id,
                oracle_rows,
            )
            consecutive_errors = (
                consecutive_errors + 1
                if row["grading_bucket"] == "Infrastructure Error"
                else 0
            )
            logger.info(
                "%s -> %s (%.1f ms)",
                case["id"], row["grading_bucket"], row["latency_ms"],
            )
        except (
            InfrastructureError,
            ChatStreamError,
            aiomysql.MySQLError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            is_infrastructure_error = isinstance(exc, InfrastructureError)
            if is_infrastructure_error:
                consecutive_errors += 1
                bucket = "Infrastructure Error"
                checks = {
                    "infrastructure": {
                        "status": "not_evaluated",
                        "code": exc.code,
                        "stage": exc.stage,
                        "detail": str(exc),
                    }
                }
            else:
                consecutive_errors += 1
                bucket = "Not evaluated"
                checks = {}
            failure_trace = write_case_trace(
                trace_dir,
                run_id,
                case,
                {
                    "request_id": None,
                    "events": [],
                    "result": {
                        "status": (
                            "infrastructure_error"
                            if is_infrastructure_error
                            else "error"
                        ),
                        "message": str(exc),
                    },
                    "expected_rows": oracle_rows,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                {
                    "checks": checks,
                    "grading_bucket": bucket,
                    "safety_leak": False,
                    "trace_payload_available": False,
                },
                REPOSITORY_ROOT,
            )
            row = {
                "case_id": case["id"],
                "split": case["split"],
                "priority": case.get("priority"),
                "grading_bucket": bucket,
                "safety_leak": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "schema_precision": None,
                "schema_recall": None,
                "checks": checks,
                "trace_path": str(failure_trace),
                "error": f"{type(exc).__name__}: {exc}",
            }
            logger.error("Case %s failed: %s: %s", case["id"], type(exc).__name__, exc)
            if is_infrastructure_error:
                logger.error(
                    "Case %s is excluded from accuracy scoring due to infrastructure failure (%s).",
                    case["id"],
                    exc.code,
                )
        rows.append(row)
        if consecutive_errors >= args.max_consecutive_errors:
            logger.error("Stopping after consecutive case or infrastructure errors.")
            break
        if index + 1 < len(selected) and args.delay > 0:
            await asyncio.sleep(args.delay)

    json_report, csv_report = write_run_reports(output_dir, run_id, rows)
    leak_count = sum(bool(row["safety_leak"]) for row in rows)
    invalid_test_count = sum(
        row.get("grading_bucket") == "Invalid Test" for row in rows
    )
    infrastructure_error_count = sum(
        row.get("grading_bucket") == "Infrastructure Error" for row in rows
    )
    logger.info(
        "Completed %d/%d cases. Invalid tests: %d. Infrastructure errors: %d. "
        "Safety leaks: %d. "
        "Reports: %s, %s",
        len(rows),
        len(selected),
        invalid_test_count,
        infrastructure_error_count,
        leak_count,
        json_report,
        csv_report,
    )
    return 1 if leak_count or invalid_test_count or infrastructure_error_count else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="API-driven Text-to-SQL test_2 runner")
    parser.add_argument("--mode", choices=("tuning", "exam"), required=True)
    parser.add_argument(
        "--case-file", default=str(CASE_FILE), help="Path to case_main.json."
    )
    parser.add_argument(
        "--case-ids",
        default="",
        help="Optional comma-separated IDs; IDs must belong to the selected split.",
    )
    parser.add_argument(
        "--backend-url",
        default=os.getenv("ASK_YOUR_DATA_BACKEND", "http://localhost:8000"),
    )
    parser.add_argument("--timeout", type=float, default=100.0)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    parser.add_argument("--trace-dir", default=str(TEST_DIR / "traces"))
    parser.add_argument("--output-dir", default=str(TEST_DIR / "reports"))
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        exit_code = asyncio.run(run(args))
    except (ValueError, OSError) as exc:
        logger.error("%s", exc)
        sys.exit(2)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
