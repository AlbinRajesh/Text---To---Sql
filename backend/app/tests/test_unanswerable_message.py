"""The model's raw reasoning must never reach the user-facing refusal text."""

import asyncio

import pytest

from app.agent.nodes.generate_query import UnanswerableQuestionError, generate_query
from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output

SCHEMA = {"users": [{"column": "id", "data_type": "bigint", "key": "PRI"}]}
RAW = "Wait! shop_details has no city column; secret_column_x is missing."


def test_unanswerable_text_is_safe_and_raw_reasoning_is_kept_internally(monkeypatch):
    class StubLLM:
        async def call_agent_json(self, *args, **kwargs):
            return {
                "answerable": False,
                "reasoning": RAW,
                "unanswerable_evidence": [
                    {"type": "table", "table": "shop_details"},
                ],
            }

    monkeypatch.setattr(
        "app.agent.nodes.generate_query.get_llm_service", lambda: StubLLM()
    )

    with pytest.raises(UnanswerableQuestionError) as info:
        asyncio.run(generate_query("Show every user.", SCHEMA, [], []))

    assert "secret_column_x" not in str(info.value)
    assert "secret_column_x" not in info.value.reasoning
    assert info.value.internal_reasoning == RAW


@pytest.mark.parametrize(
    "evidence",
    [
        {"type": "relationship", "tables": ["shop_details", "user_address"]},
        {"type": "table", "table": "shop_phone_directory"},
    ],
)
def test_missing_schema_fact_cannot_refuse_a_direct_entity_field(evidence):
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar"},
            {"column": "phone", "data_type": "varchar"},
        ],
        "user_address": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "phone", "data_type": "varchar"},
        ],
    }
    refusal = {
        "answerable": False,
        "reasoning": "A shop phone number requires joining user_address.",
        "unanswerable_evidence": [evidence],
    }

    with pytest.raises(
        QueryGenerationError,
        match="available directly on one supplied table",
    ):
        _validate_llm_output(
            refusal,
            schema,
            relationships=[],
            question="Give me the phone number for A1 Juice house.",
        )


def test_invalid_direct_field_refusal_is_retried_as_a_query(monkeypatch):
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar"},
            {"column": "phone", "data_type": "varchar"},
            {"column": "status", "data_type": "tinyint"},
        ],
        "user_address": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "phone", "data_type": "varchar"},
        ],
    }

    class StubLLM:
        calls = 0

        async def call_agent_json(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "answerable": False,
                    "reasoning": "A shop phone number requires joining user_address.",
                    "unanswerable_evidence": [{
                        "type": "relationship",
                        "tables": ["shop_details", "user_address"],
                    }],
                }
            return {
                "answerable": True,
                "reasoning": "Read the shop's phone directly.",
                "table": "shop_details",
                "joins": [],
                "select_items": [{
                    "table": "shop_details",
                    "column": "phone",
                    "aggregate": None,
                    "alias": "phone",
                }],
                "where_expression": {
                    "table": "shop_details",
                    "column": "name",
                    "operator": "=",
                    "value": "A1 Juice house",
                },
                "group_by": [],
                "having_expression": None,
                "count_groups": False,
                "order_by": [],
            }

    llm = StubLLM()
    monkeypatch.setattr(
        "app.agent.nodes.generate_query.get_llm_service", lambda: llm
    )

    result = asyncio.run(
        generate_query(
            "Give me the phone number for A1 Juice house.",
            schema,
            [],
            relationships=[],
        )
    )

    assert llm.calls == 2
    assert result["table"] == "shop_details"
    assert result["select_items"][0]["column"] == "phone"