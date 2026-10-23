from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_metadata(repository_root: Path, trace: dict[str, Any]) -> dict[str, Any]:
    prompt_dir = repository_root / "backend" / "app" / "agent" / "prompts"
    rules_file = repository_root / "backend" / "app" / "rules" / "business_rules.json"
    try:
        commit = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None

    prompt_hashes = {
        path.name: _sha256(path)
        for path in sorted(prompt_dir.glob("*.py"))
    }
    model_versions = sorted({
        str(call["model"])
        for call in trace.get("llm_calls", [])
        if isinstance(call, dict) and call.get("model")
    }) if isinstance(trace.get("llm_calls"), list) else []

    return {
        "model_version": model_versions or None,
        "prompt_version": prompt_hashes or None,
        "rules_version": _sha256(rules_file),
        "code_commit": commit,
    }


def write_case_trace(
    trace_dir: Path,
    run_id: str,
    case: dict[str, Any],
    capture: dict[str, Any],
    evaluation: dict[str, Any],
    repository_root: Path,
) -> Path:
    output_dir = trace_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result = capture.get("result") or {}
    events = capture.get("events", [])
    trace_payload = extract_trace_payload(events, result)
    pipeline_diagnostics = load_pipeline_diagnostics(
        capture.get("request_id"), repository_root
    )
    turns = capture.get("turns", [])
    diagnostic_events = list(events)
    for turn in turns:
        if isinstance(turn, dict) and isinstance(turn.get("events"), list):
            diagnostic_events.extend(turn["events"])
    generated_sql = _extract_intermediate_value(
        ("generated_sql", "compiled_sql", "safe_sql"),
        trace_payload,
        result,
        diagnostic_events,
    ) or pipeline_diagnostics.get("generated_sql")
    query_plan = _extract_intermediate_value(
        ("query_plan", "generated_plan", "plan"),
        trace_payload,
        result,
        diagnostic_events,
    ) or pipeline_diagnostics.get("query_plan")
    schema_grader_output = _extract_intermediate_value(
        (
            "schema_grader_output",
            "schema_validation",
            "grade_schema",
            "schema_grade",
            "schema_grader",
        ),
        trace_payload,
        result,
        diagnostic_events,
    ) or pipeline_diagnostics.get("schema_attempts")
    record = {
        "run_id": run_id,
        "case_id": case["id"],
        "split": case["split"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": case["question"],
        "golden_sql": case.get("golden_sql"),
        "expected_rows": capture.get("expected_rows"),
        "actual_rows": (
            result.get("results")
            if isinstance(result.get("results"), list)
            else None
        ),
        "request_id": capture.get("request_id"),
        "latency_ms": capture.get("latency_ms"),
        "generated_sql": generated_sql,
        "query_plan": query_plan,
        "schema_grader_output": schema_grader_output,
        "retrieved_tables": _latest_retrieved_tables(pipeline_diagnostics),
        "matched_business_rules": pipeline_diagnostics.get(
            "matched_business_rules"
        ),
        "validation_attempts": pipeline_diagnostics.get("validation_attempts", []),
        "safety_results": pipeline_diagnostics.get("safety_results", []),
        "refusal": pipeline_diagnostics.get("refusal"),
        "infrastructure_errors": pipeline_diagnostics.get(
            "infrastructure_errors", []
        ),
        "pipeline_diagnostics": pipeline_diagnostics or None,
        "intermediate_outputs_available": any(
            value is not None
            for value in (generated_sql, query_plan, schema_grader_output)
        ),
        "error": capture.get("error"),
        "events": events,
        "turns": turns,
        "result": result,
        "evaluation": evaluation,
        "versions": version_metadata(repository_root, trace_payload),
    }
    path = output_dir / f"{case['id']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def load_pipeline_diagnostics(
    request_id: Any, repository_root: Path
) -> dict[str, Any]:
    if not isinstance(request_id, str) or not request_id:
        return {}
    configured = os.getenv("TEXT_TO_SQL_TRACE_DIR")
    directory = (
        Path(configured)
        if configured
        else repository_root / "backend" / "logs" / "pipeline_traces"
    )
    path = directory / f"{request_id}.json"
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        import logging

        logging.getLogger(__name__).exception(
            "Could not load persisted pipeline trace for request_id=%s", request_id
        )
        return {}
    diagnostics = trace.get("diagnostics")
    return diagnostics if isinstance(diagnostics, dict) else {}


def _latest_retrieved_tables(diagnostics: dict[str, Any]) -> list[str]:
    attempts = diagnostics.get("schema_attempts")
    if not isinstance(attempts, list):
        return []
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and isinstance(
            attempt.get("retrieved_tables"), list
        ):
            return attempt["retrieved_tables"]
    return []


def extract_trace_payload(
    events: list[dict[str, Any]], result: dict[str, Any]
) -> dict[str, Any]:
    from app.tests.test_2.evaluators import extract_trace

    return extract_trace(events, result)


def _extract_intermediate_value(
    candidate_keys: tuple[str, ...],
    trace_payload: dict[str, Any],
    result: dict[str, Any],
    events: list[dict[str, Any]],
) -> Any:
    sources = [trace_payload, result, *events]
    for source in sources:
        value = _find_key(source, set(candidate_keys))
        if value is not None:
            return value
    return None


def _find_key(value: Any, candidate_keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key in candidate_keys:
            if key in value and value[key] is not None:
                return value[key]
        for child in value.values():
            found = _find_key(child, candidate_keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, candidate_keys)
            if found is not None:
                return found
    return None


def write_run_reports(output_dir: Path, run_id: str, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run_id}.json"
    csv_path = output_dir / f"{run_id}.csv"

    buckets: dict[str, int] = {}
    for row in rows:
        bucket = row.get("grading_bucket", "Not evaluated")
        buckets[bucket] = buckets.get(bucket, 0) + 1
    summary = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(rows),
        "grading_buckets": buckets,
        "infrastructure_error_count": buckets.get("Infrastructure Error", 0),
        "accuracy_scored_case_count": sum(
            1
            for row in rows
            if row.get("grading_bucket")
            not in {"Infrastructure Error", "Invalid Test", "Not evaluated"}
        ),
        "safety_leak_count": sum(bool(row.get("safety_leak")) for row in rows),
        "schema_precision_mean": _mean_metric(rows, "precision"),
        "schema_recall_mean": _mean_metric(rows, "recall"),
        "cases": rows,
    }
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    fields = (
        "case_id", "split", "priority", "grading_bucket", "safety_leak",
        "latency_ms", "schema_precision", "schema_recall", "trace_path", "error",
    )
    with csv_path.open("w", newline="", encoding="utf-8-sig") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({
            "case_id": row.get("case_id"),
            "split": row.get("split"),
            "priority": row.get("priority"),
            "grading_bucket": row.get("grading_bucket"),
            "safety_leak": row.get("safety_leak"),
            "latency_ms": row.get("latency_ms"),
            "schema_precision": row.get("schema_precision"),
            "schema_recall": row.get("schema_recall"),
            "trace_path": row.get("trace_path"),
            "error": row.get("error"),
        } for row in rows)
    return json_path, csv_path


def _mean_metric(rows: list[dict[str, Any]], name: str) -> float | None:
    values = [
        row[name]
        for row in rows
        if isinstance(row.get(name), (int, float))
    ]
    return round(sum(values) / len(values), 4) if values else None
