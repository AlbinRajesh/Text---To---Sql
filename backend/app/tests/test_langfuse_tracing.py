import asyncio
from types import SimpleNamespace

from app.agent import graph
from app.services import llm_service
from app.services.llm_service import AgentConfig, LLMService


def test_run_pipeline_passes_request_scoped_langfuse_callback(monkeypatch):
    captured = {}
    handler = object()

    class FakeGraph:
        async def ainvoke(self, state, config):
            captured["state"] = state
            captured["config"] = config
            return state

    monkeypatch.setattr(graph, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(graph, "get_conversation_context", lambda _id: [])
    monkeypatch.setattr(graph, "start_request", lambda: [])
    monkeypatch.setattr(
        graph, "create_callback_handler", lambda: handler
    )

    asyncio.run(
        graph.run_pipeline(
            "show me shops",
            request_id="request-123",
            conversation_id="conversation-456",
        )
    )

    assert captured["config"]["callbacks"] == [handler]
    assert captured["config"]["configurable"]["thread_id"] == "conversation-456"
    assert captured["config"]["metadata"]["request_id"] == "request-123"


def test_run_pipeline_omits_callbacks_when_langfuse_is_not_configured(monkeypatch):
    captured = {}

    class FakeGraph:
        async def ainvoke(self, state, config):
            captured["config"] = config
            return state

    monkeypatch.setattr(graph, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(graph, "get_conversation_context", lambda _id: [])
    monkeypatch.setattr(graph, "start_request", lambda: [])
    monkeypatch.setattr(graph, "create_callback_handler", lambda: None)

    asyncio.run(graph.run_pipeline("show me shops", conversation_id="thread"))

    assert "callbacks" not in captured["config"]
    assert captured["config"]["configurable"]["thread_id"] == "thread"


def test_groq_generation_records_provider_token_usage(monkeypatch):
    completed = []
    monkeypatch.setattr(
        llm_service,
        "start_llm_generation",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        llm_service,
        "finish_llm_generation",
        lambda observation, **kwargs: completed.append((observation, kwargs)),
    )

    class Completions:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
                usage=SimpleNamespace(
                    prompt_tokens=11,
                    completion_tokens=7,
                    total_tokens=18,
                ),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    service = LLMService()
    config = AgentConfig("CLASSIFIER", "groq", "test-model", "unused")

    answer = asyncio.run(
        service._call_groq(
            config,
            client,
            [{"role": "user", "content": "question"}],
            temperature=0.0,
            json_mode=False,
            max_tokens=None,
        )
    )

    assert answer == "answer"
    assert completed[0][1]["usage_details"] == {
        "input": 11,
        "output": 7,
        "total": 18,
    }


def test_usage_details_ignores_missing_and_invalid_counts():
    assert llm_service._usage_details(3, 2, None) == {
        "input": 3,
        "output": 2,
        "total": 5,
    }
    assert llm_service._usage_details(None, -1, "5") == {}
