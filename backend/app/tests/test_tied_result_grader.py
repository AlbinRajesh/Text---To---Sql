import asyncio

from app.agent.nodes.result_grader import grade_result


def test_complete_tied_ranked_results_pass_without_llm_rejection(monkeypatch):
    def unexpected_llm():
        raise AssertionError("Validated, complete tie evidence should be deterministic.")

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service",
        unexpected_llm,
    )

    verdict = asyncio.run(
        grade_result(
            "Which month had the most completed bookings in 2025?",
            {
                "columns": ["booking_month", "booking_count"],
                "results": [
                    {"booking_month": "May", "booking_count": 8},
                    {"booking_month": "November", "booking_count": 8},
                ],
                "count": 2,
                "tie_at_limit": True,
                "truncated": False,
            },
            ranking={
                "metric": "booking_count",
                "column": "booking_count",
                "direction": "DESC",
                "limit": 1,
            },
        )
    )

    assert verdict == {
        "is_valid": True,
        "flagged": False,
        "flag_reason": None,
    }


def test_tied_result_is_not_auto_accepted_when_truncated(monkeypatch):
    class FakeLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return {"is_valid": False, "flag_reason": "ordinary grade"}

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service",
        lambda: FakeLLM(),
    )

    verdict = asyncio.run(
        grade_result(
            "Which month had the most bookings?",
            {
                "columns": ["month", "booking_count"],
                "results": [
                    {"month": "May", "booking_count": 8},
                    {"month": "November", "booking_count": 8},
                ],
                "count": 2,
                "tie_at_limit": True,
                "truncated": True,
            },
            ranking={
                "metric": "booking_count",
                "column": "booking_count",
                "direction": "DESC",
                "limit": 1,
            },
        )
    )

    assert verdict["is_valid"] is False
    assert verdict["flag_reason"] == "ordinary grade"
