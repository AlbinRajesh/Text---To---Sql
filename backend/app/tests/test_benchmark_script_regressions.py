import asyncio
import csv
from decimal import Decimal

from app.tests import advanced_query_generation_benchmark as advanced
from app.tests import master_golden_benchmark as master
from app.tests import query_generation_benchmark as basic


def test_basic_benchmark_main_parses_case_selection(monkeypatch, tmp_path):
    captured = {}

    async def fake_run(output_path, selected=None, *, execute=False):
        captured.update(selected=selected, execute=execute)

    original_asyncio_run = asyncio.run

    def fake_asyncio_run(coroutine):
        return original_asyncio_run(coroutine)

    monkeypatch.setattr(basic, "_run", fake_run)
    monkeypatch.setattr(basic.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "query_generation_benchmark",
            "--cases",
            "7,19-20",
            "--output",
            str(tmp_path / "result.csv"),
        ],
    )

    basic.main()

    assert captured == {"selected": {7, 19, 20}, "execute": False}


def test_advanced_benchmark_connects_replica_before_cases(monkeypatch, tmp_path):
    events = []

    async def fake_primary():
        events.append("primary")

    async def fake_replica():
        events.append("replica")

    async def fake_refresh():
        events.append("refresh")

    async def fake_index(_schema):
        events.append("index")

    async def fake_case(_case):
        assert "replica" in events
        events.append("case")
        return {
            "plan_match": "yes",
            "safety_validation": "passed",
            "explain_validation": "passed",
            "generation_status": "generated",
        }

    async def fake_close():
        events.append("close")

    monkeypatch.setattr(advanced, "connect_to_mysql", fake_primary)
    monkeypatch.setattr(advanced, "connect_to_read_replica", fake_replica)
    monkeypatch.setattr(advanced, "close_mysql_connection", fake_close)
    monkeypatch.setattr(advanced, "refresh_schema_cache", fake_refresh)
    monkeypatch.setattr(advanced, "get_cached_schema", lambda: {})
    monkeypatch.setattr(advanced, "build_schema_index", fake_index)
    monkeypatch.setattr(advanced, "_run_case", fake_case)
    monkeypatch.setattr(
        advanced,
        "CASES",
        [{"number": 1, "expected_outcome": "sql", "reference_note": ""}],
    )

    asyncio.run(
        advanced._run(tmp_path / "advanced.csv", selected={1})
    )

    assert events == ["primary", "replica", "refresh", "index", "case", "close"]


def test_master_benchmark_logs_case_35_compiled_bind_values(
    monkeypatch, tmp_path, caplog
):
    async def classify(_question):
        return {"intent": "data", "rewritten_question": "discount above 50%"}

    async def grade(_question):
        return {
            "schema": {"offer": [{"column": "id", "data_type": "bigint"}]},
            "relationships": [],
        }

    async def generate(*_args):
        return {"table": "offer"}

    monkeypatch.setattr(master, "classify_and_rewrite", classify)
    monkeypatch.setattr(master, "grade_schema_sufficiency", grade)
    monkeypatch.setattr(master, "find_relevant_rules", lambda _question: [])
    monkeypatch.setattr(master, "generate_query", generate)
    monkeypatch.setattr(
        master,
        "run_safety_check",
        lambda *_args, **_kwargs: {
            "sql": "SELECT ... WHERE ...",
            "params": [Decimal("50"), Decimal("1"), Decimal("0"), 1000],
        },
    )
    monkeypatch.setattr(
        master, "evaluate_canonical_ast", lambda *_args: (True, "match")
    )
    caplog.set_level("INFO", logger=master.logger.name)

    row = asyncio.run(
        master._run_single_case(
            {
                "number": 35,
                "category": "complex",
                "question": "Which offers have discount above 50%?",
                "expected_outcome": "sql",
                "golden_sql": "SELECT 1",
                "note": "",
            },
            tmp_path,
        )
    )

    assert row["compiled_params"] == '["50", "1", "0", 1000]'
    assert "Case #35 compiled SQL bind parameters: [\"50\", \"1\", \"0\", 1000]" in caplog.text
    assert {
        event["name"]
        for event in row["_latency_events"]
        if event.get("kind") == "node"
    } >= {
        "classify",
        "preflight",
        "schema_grade",
        "business_rules",
        "checkpoint_save",
        "generate_query",
        "safety_check",
        "eval_ast",
    }


def test_master_latency_report_contains_stage_llm_database_and_scope_rows(
    tmp_path,
):
    rows = [{
        "case_number": 31,
        "category": "moderate",
        "evaluation_tier": "TIER_2_EXECUTION_ACCURACY",
        "_latency_events": [
            {"kind": "node", "name": "generate_query", "seconds": 1.25},
            {
                "kind": "llm",
                "node": "generate_query",
                "role": "QUERY_GENERATOR",
                "provider": "groq",
                "model": "test-model",
                "seconds": 1.0,
                "attempts": 2,
                "causes": ["rate_limit_429"],
                "backoff_seconds": 0.4,
                "ok": True,
            },
            {
                "kind": "database",
                "stage": "execute_generated_sql",
                "operation": "read_replica_read",
                "pool_wait_seconds": 0.02,
                "query_fetch_seconds": 0.3,
                "row_count": 4,
                "ok": True,
            },
            {
                "kind": "case_total",
                "seconds": 2.0,
                "case_status": "TIER_2_EXECUTION_ACCURACY",
            },
        ],
    }]
    report_path = tmp_path / "benchmark_latency.csv"

    master._write_latency_report(
        report_path,
        rows,
        "run-123",
        setup_ms=100,
        throttle_wait_ms=2000,
    )

    with report_path.open(encoding="utf-8-sig", newline="") as report_file:
        report = list(csv.DictReader(report_file))

    assert any(
        row["event_type"] == "stage"
        and row["stage"] == "generate_query"
        and row["duration_ms"] == "1250.0"
        for row in report
    )
    llm_row = next(row for row in report if row["event_type"] == "llm_call")
    assert llm_row["llm_role"] == "QUERY_GENERATOR"
    assert llm_row["attempts"] == "2"
    assert llm_row["retry_wait_ms"] == "400.0"
    db_row = next(row for row in report if row["event_type"] == "database_call")
    assert db_row["pool_wait_ms"] == "20.0"
    assert db_row["query_fetch_ms"] == "300.0"
    assert db_row["row_count"] == "4"
    stage_summary = next(
        row for row in report
        if row["event_type"] == "latency_summary"
        and row["stage"] == "generate_query"
    )
    assert stage_summary["p50_ms"] == "1250.0"
    assert stage_summary["p95_ms"] == "1250.0"
    assert stage_summary["sample_count"] == "1"
    assert any(row["event_type"] == "scope_note" for row in report)


def test_master_main_writes_accuracy_and_latency_reports(
    monkeypatch, tmp_path
):
    async def no_op():
        return None

    async def fake_index(_schema):
        return None

    async def fake_case(*_args, **_kwargs):
        return {
            "case_number": 31,
            "category": "moderate",
            "question": "monthly booking totals",
            "expected_outcome": "sql",
            "evaluation_tier": "TIER_2_EXECUTION_ACCURACY",
            "result_label": "EXACT",
            "passed": "YES",
            "failed_stage": "eval_execution",
            "total_ms": 10.0,
            "compiled_sql": "",
            "compiled_params": "",
            "golden_sql": "",
            "ast_match": "no",
            "generated_rows_count": 4,
            "golden_rows_count": 4,
            "truncated": False,
            "detail_message": "",
            "error_type": "",
            "error_message": "",
            "note": "",
            "_latency_events": [{
                "kind": "case_total",
                "seconds": 0.01,
                "case_status": "TIER_2_EXECUTION_ACCURACY",
            }],
        }

    monkeypatch.setattr(master, "connect_to_mysql", no_op)
    monkeypatch.setattr(master, "connect_to_read_replica", no_op)
    monkeypatch.setattr(master, "close_mysql_connection", no_op)
    monkeypatch.setattr(master, "refresh_schema_cache", no_op)
    monkeypatch.setattr(master, "get_cached_schema", lambda: {})
    monkeypatch.setattr(master, "build_schema_index", fake_index)
    monkeypatch.setattr(master, "_run_single_case", fake_case)
    monkeypatch.setattr(
        master,
        "GOLDEN_BENCHMARK_CASES",
        [{
            "number": 31,
            "category": "moderate",
            "question": "monthly booking totals",
            "expected_outcome": "sql",
            "golden_sql": "",
            "note": "",
        }],
    )
    accuracy_path = tmp_path / "result.csv"
    monkeypatch.setattr(
        "sys.argv",
        [
            "master_golden_benchmark",
            "--output",
            str(accuracy_path),
            "--delay",
            "0",
        ],
    )

    asyncio.run(master.main())

    latency_path = tmp_path / "result_latency.csv"
    assert accuracy_path.exists()
    assert latency_path.exists()
    with accuracy_path.open(encoding="utf-8-sig", newline="") as csv_file:
        assert len(list(csv.DictReader(csv_file))) == 1
    with latency_path.open(encoding="utf-8-sig", newline="") as csv_file:
        latency_rows = list(csv.DictReader(csv_file))
    assert any(row["event_type"] == "case_total" for row in latency_rows)
