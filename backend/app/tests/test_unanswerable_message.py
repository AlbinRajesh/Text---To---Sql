"""The model's raw reasoning must never reach the user-facing refusal text."""

import asyncio

import pytest

from app.agent.nodes.generate_query import UnanswerableQuestionError, generate_query

SCHEMA = {"users": [{"column": "id", "data_type": "bigint", "key": "PRI"}]}
RAW = "Wait! shop_details has no city column; secret_column_x is missing."


def test_unanswerable_text_is_safe_and_raw_reasoning_is_kept_internally(monkeypatch):
    class StubLLM:
        async def call_agent_json(self, *args, **kwargs):
            return {"answerable": False, "reasoning": RAW}

    monkeypatch.setattr(
        "app.agent.nodes.generate_query.get_llm_service", lambda: StubLLM()
    )

    with pytest.raises(UnanswerableQuestionError) as info:
        asyncio.run(generate_query("Show every user.", SCHEMA, [], []))

    assert "secret_column_x" not in str(info.value)
    assert "secret_column_x" not in info.value.reasoning
    assert info.value.internal_reasoning == RAW