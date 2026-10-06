import asyncio

from app.agent.nodes import classifier_rewrite as cr


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


def _run(question, replies, monkeypatch):
    llm = FakeLLM(replies)
    monkeypatch.setattr(cr, "get_llm_service", lambda: llm)
    return asyncio.run(cr.classify_and_rewrite(question)), llm


def test_added_columns_are_retried(monkeypatch):
    out, llm = _run(
        "Shops in Kochi",
        [_reply("List the name, phone and website of shops in Kochi"),
         _reply("List shops in Kochi")],
        monkeypatch,
    )
    assert out["rewritten_question"] == "List shops in Kochi"
    assert llm.calls == 2


def test_from_changed_to_named_is_retried(monkeypatch):
    out, llm = _run(
        "shops from Kochi",
        [_reply("Show the shop named 'Kochi'"), _reply("Show shops from Kochi")],
        monkeypatch,
    )
    assert out["rewritten_question"] == "Show shops from Kochi"
    assert llm.calls == 2


def test_still_wrong_after_retry_uses_original(monkeypatch):
    bad = _reply("List the phone of shops in Kochi")
    out, llm = _run("Shops in Kochi", [bad, bad], monkeypatch)
    assert out["rewritten_question"] == "Shops in Kochi"
    assert llm.calls == 2


def test_subcategory_named_is_allowed(monkeypatch):
    out, llm = _run(
        "show me restaurants",
        [_reply("Show shops in the subcategory named 'Restaurent'")],
        monkeypatch,
    )
    assert llm.calls == 1


def test_user_asked_for_phone_is_allowed(monkeypatch):
    out, llm = _run(
        "phone of shops in Kochi",
        [_reply("List the phone of shops in Kochi")],
        monkeypatch,
    )
    assert llm.calls == 1