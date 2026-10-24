import asyncio

from app.agent.nodes import return_save


def test_build_answer_preserves_empty_result_details(monkeypatch):
    async def fake_summary(final_answer, _original_question):
        final_answer["summary"] = "No matching records were found."

    monkeypatch.setattr(return_save, "_attach_summary", fake_summary)
    state = {
        "classifier_result": {
            "rewritten_question": "Find matching shops",
            "original_question": "Find matching shops",
            "intent": "data",
        },
        "execution_result": {
            "results": [],
            "columns": ["name"],
            "count": 0,
            "row_count": 0,
        },
        "generated_query": {},
        "result_grader_verdict": {"is_valid": True},
        "escalated": False,
    }

    answer = asyncio.run(return_save.build_answer(state))

    assert answer["answer_type"] == "empty"
    assert answer["results"] == []
    assert answer["summary"] == "No matching records were found."


def test_build_answer_returns_valid_results_and_summary(monkeypatch):
    async def fake_summary(final_answer, _original_question):
        final_answer["summary"] = "One shop matched."

    monkeypatch.setattr(return_save, "_attach_summary", fake_summary)
    state = {
        "classifier_result": {
            "rewritten_question": "Find shops",
            "original_question": "Find shops",
            "intent": "data",
        },
        "execution_result": {
            "results": [{"name": "Sample shop"}],
            "columns": ["name"],
            "count": 1,
            "row_count": 1,
        },
        "generated_query": {},
        "result_grader_verdict": {"is_valid": True},
        "escalated": False,
    }

    answer = asyncio.run(return_save.build_answer(state))

    assert answer["results"] == [{"name": "Sample shop"}]
    assert answer["summary"] == "One shop matched."
