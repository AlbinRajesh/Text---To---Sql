import asyncio

from app.agent.graph import _build_pipeline_diagnostics
from types import SimpleNamespace

from app.agent import graph


def test_pipeline_diagnostics_capture_execution_context(monkeypatch):
    schema = {"views": [{"column": "created_at", "data_type": "datetime"}]}
    relationships = []
    plan = {"table": "views", "where_expression": None}
    safety_result = {"sql": "SELECT 1", "params": [1]}
    execution_result = {"row_count": 3}
    monkeypatch.setenv("MYSQL_DATABASE", "benchmark_db")

    diagnostics = _build_pipeline_diagnostics({
        "generated_query": plan,
        "safety_check_result": safety_result,
        "execution_result": execution_result,
        "schema_context": schema,
        "schema_relationships": relationships,
    })

    assert diagnostics["query_plan"] == plan
    assert diagnostics["generated_sql"] == "SELECT 1"
    assert diagnostics["generated_parameters"] == [1]
    assert diagnostics["execution_database"] == "benchmark_db"
    assert diagnostics["execution_row_count"] == 3
    assert diagnostics["schema_context"] == schema
    assert diagnostics["schema_relationships"] == relationships
    assert diagnostics["schema_fingerprint"]
    assert diagnostics["relationship_fingerprint"]


def test_pipeline_diagnostics_preserve_redacted_query_validation_attempts():
    attempts = [{
        "attempt": 1,
        "error_code": "QueryGenerationError",
        "stage": "generator",
        "plan_summary": {
            "reason": "validation_rejected",
            "plan": {"table": "users"},
        },
    }]

    diagnostics = _build_pipeline_diagnostics({
        "query_validation_attempts": attempts,
    })

    assert diagnostics["query_validation_attempts"] == attempts


def test_query_generation_failure_copies_validation_attempts_to_state(monkeypatch):
    attempts = [{
        "attempt": 1,
        "error_code": "QueryGenerationError",
        "stage": "generator",
        "plan_summary": {"reason": "validation_rejected", "plan": {}},
    }]

    monkeypatch.setattr(graph, "resolve_value_candidates", lambda *_args, **_kwargs: [])

    async def fail_generation(*_args, **_kwargs):
        raise graph.QueryGenerationError(
            "Plan validation failed.",
            validation_attempts=attempts,
        )

    monkeypatch.setattr(graph, "generate_query", fail_generation)
    state = {
        "classifier_result": {
            "rewritten_question": "How many partners?",
            "original_question": "How many partners?",
        },
        "schema_context": {"users": [{"column": "id"}]},
        "matched_business_rules": [],
        "schema_relationships": [],
    }

    result = asyncio.run(graph.node_generate_query(state))

    assert result["query_validation_attempts"] == attempts


def test_refusal_diagnostics_include_full_schema_and_runtime_versions(monkeypatch):
    schema = {"views": [{"column": "created_at", "data_type": "datetime"}]}
    monkeypatch.setattr(
        graph,
        "schema_cache",
        SimpleNamespace(
            tables=schema,
            last_refreshed=1234.5,
            is_stale=lambda: False,
        ),
    )
    monkeypatch.setattr(graph, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(graph, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(
        graph,
        "get_schema_index_metadata",
        lambda: {
            "built": True,
            "table_count": 1,
            "tables": ["views"],
            "schema_fingerprint": "index-schema-sha",
        },
    )
    monkeypatch.setattr(
        graph,
        "get_business_rules_store",
        lambda: SimpleNamespace(version="rulebook-sha"),
    )
    monkeypatch.setenv("APP_BUILD_ID", "build-sha")

    diagnostics = _build_pipeline_diagnostics({
        "unanswerable_reasoning": "Schema was insufficient.",
    })

    assert diagnostics["build_id"] == "build-sha"
    assert diagnostics["business_rulebook_version"] == "rulebook-sha"
    assert diagnostics["schema_catalog"]["table_count"] == 1
    assert diagnostics["schema_catalog"]["last_refreshed"] == 1234.5
    assert diagnostics["schema_catalog"]["stale"] is False
    assert diagnostics["schema_catalog"]["schema_fingerprint"]
    assert diagnostics["schema_index"]["tables"] == ["views"]
    assert diagnostics["schema_index"]["schema_fingerprint"] == "index-schema-sha"
    assert diagnostics["schema_index"]["matches_catalog"] is False
    assert "schema_context" not in diagnostics
    assert diagnostics["refusal"]["reason"] == "Schema was insufficient."
