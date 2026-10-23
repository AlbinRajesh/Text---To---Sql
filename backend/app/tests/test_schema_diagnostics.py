import asyncio

from app.agent.nodes import grade_sample


def test_schema_attempt_captures_retrieved_tables_and_missing_items(monkeypatch):
    schema = {"users": [{"column": "id", "key": "PRI"}]}

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: [])

    async def retrieve(_question, top_k):
        return ["users"]

    async def sufficient(*args, **kwargs):
        return False, [{"type": "column", "detail": "missing field", "tables": ["users"]}]

    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(
        grade_sample, "_ask_cheap_model_if_sufficient", sufficient
    )

    diagnostics = {
        "schema_attempts": [],
        "infrastructure_errors": [],
    }
    try:
        asyncio.run(
            grade_sample.grade_schema_sufficiency(
                "diagnostic test",
                diagnostics=diagnostics,
            )
        )
    except grade_sample.GradeSampleError:
        pass

    assert diagnostics["schema_attempts"]
    assert diagnostics["schema_attempts"][0]["retrieved_tables"] == ["users"]
    assert diagnostics["schema_attempts"][0]["missing_items"] == [
        {"type": "column", "detail": "missing field", "tables": ["users"]}
    ]


def test_schema_grader_raw_text_is_persisted(monkeypatch):
    raw_output = '{"sufficient":false,"missing":["users.id"]}'

    class FakeLLM:
        async def call_agent_json_with_raw(self, *args, **kwargs):
            return {"sufficient": False, "missing": ["users.id"]}, raw_output

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service",
        lambda: FakeLLM(),
    )
    diagnostics = {"schema_attempts": [{}]}

    with grade_sample.schema_diagnostics_scope(diagnostics):
        asyncio.run(
            grade_sample._ask_cheap_model_if_sufficient(
                "diagnostic test",
                "diagnostic test",
                "users: id",
                [],
            )
        )

    assert diagnostics["schema_attempts"][0]["grader_raw_output"] == raw_output
