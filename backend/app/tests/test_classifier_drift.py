import asyncio

from app.agent.nodes import classifier_rewrite as cr


SCHEMA_WITH_OFFER_DESCRIPTION = {
    "shop_details": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "offer": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "description", "data_type": "longtext", "key": ""},
    ],
}


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def call_agent_json(self, agent, messages, temperature=0.0):
        self.calls += 1
        return self.replies.pop(0)


def _reply(rewrite):
    return {
        "intent": "data",
        "complexity": "simple",
        "rewritten_question": rewrite,
        "chat_reply": "",
        "reasoning": "x",
    }


def _run(question, replies, monkeypatch, schema=SCHEMA_WITH_OFFER_DESCRIPTION):
    llm = FakeLLM(replies)
    monkeypatch.setattr(cr, "get_llm_service", lambda: llm)
    monkeypatch.setattr(cr, "get_cached_schema", lambda: schema)
    return asyncio.run(cr.classify_and_rewrite(question)), llm


def test_schema_field_word_added_by_rewrite_falls_back_without_retry(monkeypatch):
    bad = _reply("Show shops whose name or description includes 'fish meals'")

    result, llm = _run("who sells fish meals?", [bad], monkeypatch)

    assert result["rewritten_question"] == "who sells fish meals?"
    assert llm.calls == 1


def test_added_schema_table_word_falls_back_to_original(monkeypatch):
    bad = _reply("Search the offer for 'fish meals'")

    result, llm = _run("who sells fish meals?", [bad], monkeypatch)

    assert result["rewritten_question"] == "who sells fish meals?"
    assert llm.calls == 1


def test_schema_words_already_in_original_are_allowed(monkeypatch):
    rewrite = "Show me the description of this offer"
    result, llm = _run(
        "show me the offer description",
        [_reply(rewrite)],
        monkeypatch,
    )

    assert result["rewritten_question"] == rewrite
    assert llm.calls == 1


def test_short_schema_word_is_ignored():
    schema = {
        "shop_address": [
            {"column": "is_default", "data_type": "tinyint", "key": ""},
        ]
    }

    assert cr._find_rewrite_drift(
        "show shops", "show shops in town", schema
    ) is None
    assert "is" not in cr._schema_terms(schema)


def test_description_schema_word_is_still_detected():
    drift = cr._find_rewrite_drift(
        "who sells fish meals?",
        "Show shops with a description matching fish meals",
        SCHEMA_WITH_OFFER_DESCRIPTION,
    )

    assert drift == "added schema field or table word 'description'"


def test_location_rewritten_as_named_falls_back_without_retry(monkeypatch):
    original = "shops from Kochi"
    rewrite = "Show the shop named 'Kochi'"
    drift = cr._find_rewrite_drift(original, rewrite, SCHEMA_WITH_OFFER_DESCRIPTION)
    result, llm = _run(
        original,
        [_reply(rewrite)],
        monkeypatch,
    )

    assert drift and "named" in drift
    assert result["rewritten_question"] == original
    assert llm.calls == 1


def test_value_rewritten_as_called_falls_back_without_retry(monkeypatch):
    original = "offers in Nagercoil"
    rewrite = "Show offers called 'Nagercoil'"
    drift = cr._find_rewrite_drift(original, rewrite, SCHEMA_WITH_OFFER_DESCRIPTION)
    result, llm = _run(
        original,
        [_reply(rewrite)],
        monkeypatch,
    )

    assert drift and "named" in drift
    assert result["rewritten_question"] == original
    assert llm.calls == 1


def test_missing_schema_cache_uses_original_without_retry(monkeypatch):
    from app.agent.nodes.schema_grounding import SchemaGroundingError

    def no_schema():
        raise SchemaGroundingError("cache not initialized")

    llm = FakeLLM([_reply("Show shops with a description matching fish")])
    monkeypatch.setattr(cr, "get_llm_service", lambda: llm)
    monkeypatch.setattr(cr, "get_cached_schema", no_schema)

    result = asyncio.run(cr.classify_and_rewrite("who sells fish meals?"))

    assert result["rewritten_question"] == "who sells fish meals?"
    assert llm.calls == 1
