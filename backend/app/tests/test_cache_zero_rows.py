import asyncio

from app.agent.nodes import cache_check, return_save


def _state(rows, no_data_reason=None):
    execution = {
        "results": rows,
        "columns": ["phone"],
        "count": len(rows),
        "row_count": len(rows),
    }
    if no_data_reason:
        execution["no_data_reason"] = no_data_reason
    return {
        "classifier_result": {
            "rewritten_question": "q",
            "original_question": "q",
            "intent": "data",
        },
        "execution_result": execution,
        "generated_query": {},
        "result_grader_verdict": {"is_valid": True},
        "escalated": False,
    }


def _run(state, monkeypatch):
    stored = []

    async def fake_store(question, answer, version):
        stored.append(question)

    async def fake_summary(final_answer, original_question):
        return None

    monkeypatch.setattr(return_save, "cache_store", fake_store)
    monkeypatch.setattr(return_save, "_attach_summary", fake_summary)
    out = asyncio.run(return_save.build_and_save_answer(state))
    return out, stored


def test_zero_row_result_is_not_cached(monkeypatch):
    out, stored = _run(_state([]), monkeypatch)
    assert stored == []
    assert out["saved_to_cache"] is False
    assert out["final_answer"]["answer_type"] == "empty"
    assert out["final_answer"]["message"] == "No data found for this request."


def test_empty_aggregate_outcome_is_not_cached(monkeypatch):
    out, stored = _run(_state([{"total": None}], "no_rows"), monkeypatch)
    assert stored == []
    assert out["saved_to_cache"] is False


def test_result_with_rows_is_still_cached(monkeypatch):
    out, stored = _run(_state([{"phone": "9487624791"}]), monkeypatch)
    assert stored == ["q"]
    assert out["saved_to_cache"] is True


def test_cache_store_refuses_empty_answer(monkeypatch):
    async def fake_embed(text):
        return [1.0]

    monkeypatch.setattr(cache_check, "embed_single_text", fake_embed)
    monkeypatch.setattr(cache_check, "_cache", [])
    asyncio.run(cache_check.store("q", {"answer_type": "empty"}))
    assert cache_check._cache == []