import json

from app.services.trace_store import persist_pipeline_trace
from app.tests.test_2.reporting import write_case_trace


def test_trace_records_available_intermediate_outputs(tmp_path):
    case = {
        "id": "T999",
        "split": "tuning",
        "question": "Test question",
        "golden_sql": "SELECT 1",
    }
    capture = {
        "request_id": "request-1",
        "latency_ms": 42.0,
        "events": [
            {
                "type": "debug_trace",
                "data": {
                    "generated_sql": "SELECT 1",
                    "query_plan": {"table": "users"},
                    "schema_validation": {"tables": ["users"]},
                },
            }
        ],
        "expected_rows": [{"id": 1}],
        "result": {"status": "ok", "results": [{"1": 1}]},
    }

    path = write_case_trace(
        tmp_path / "traces",
        "run-1",
        case,
        capture,
        {"grading_bucket": "Correct"},
        tmp_path,
    )
    trace = json.loads(path.read_text(encoding="utf-8"))

    assert trace["golden_sql"] == "SELECT 1"
    assert trace["latency_ms"] == 42.0
    assert trace["generated_sql"] == "SELECT 1"
    assert trace["query_plan"] == {"table": "users"}
    assert trace["schema_grader_output"] == {"tables": ["users"]}
    assert trace["intermediate_outputs_available"] is True
    assert trace["expected_rows"] == [{"id": 1}]
    assert trace["actual_rows"] == [{"1": 1}]


def test_trace_merges_persisted_pipeline_diagnostics(tmp_path, monkeypatch):
    trace_dir = tmp_path / "pipeline_traces"
    trace_dir.mkdir()
    (trace_dir / "request-2.json").write_text(
        json.dumps({
            "request_id": "request-2",
            "diagnostics": {
                "schema_attempts": [{
                    "retrieved_tables": ["users", "plans"],
                    "grader_raw_output": {
                        "sufficient": False,
                        "missing": [{"type": "relationship", "tables": ["users", "plans"]}],
                    },
                }],
                "matched_business_rules": [{"term": "subscription_plan"}],
                "query_plan": {"table": "users"},
                "generated_sql": "SELECT COUNT(*) FROM users",
                "validation_attempts": [{"attempt": 1, "status": "validated"}],
                "safety_results": [{"passed": True, "rule_code": None}],
                "refusal": None,
                "infrastructure_errors": [],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_TRACE_DIR", str(trace_dir))

    path = write_case_trace(
        tmp_path / "traces",
        "run-2",
        {"id": "T100", "split": "tuning", "question": "Question"},
        {
            "request_id": "request-2",
            "events": [],
            "result": {"status": "ok"},
        },
        {},
        tmp_path,
    )
    trace = json.loads(path.read_text(encoding="utf-8"))

    assert trace["retrieved_tables"] == ["users", "plans"]
    assert trace["matched_business_rules"] == [{"term": "subscription_plan"}]
    assert trace["generated_sql"] == "SELECT COUNT(*) FROM users"
    assert trace["query_plan"] == {"table": "users"}
    assert trace["validation_attempts"][0]["attempt"] == 1
    assert trace["safety_results"][0]["passed"] is True


def test_backend_persists_diagnostics_by_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_TRACE_DIR", str(tmp_path))
    state = {
        "request_id": "request-3",
        "user_question": "How many users?",
        "classifier_result": {
            "intent": "data",
            "rewritten_question": "Count users",
            "catalog_filters": [{"value": "private"}],
        },
        "diagnostics": {
            "schema_attempts": [{"retrieved_tables": ["users"]}],
            "infrastructure_errors": [],
        },
        "timings": [],
    }

    path = persist_pipeline_trace(state)
    trace = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "request-3.json"
    assert trace["diagnostics"]["schema_attempts"][0]["retrieved_tables"] == [
        "users"
    ]
    assert trace["classifier_result"] == {
        "intent": "data",
        "rewritten_question": "Count users",
    }
