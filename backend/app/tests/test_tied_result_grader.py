import asyncio
from datetime import date, datetime, timezone

import pytest

from app.agent.nodes.result_grader import (
    _ranked_result_violation,
    grade_result,
)
from app.agent.prompts.grader_prompt import ranked_result_metadata


@pytest.mark.parametrize(
    ("column", "values"),
    [
        ("registered_date", ["2026-10-03", "2026-10-01"]),
        (
            "last_updated",
            ["2026-10-03T12:30:00Z", "2026-10-03T11:30:00Z"],
        ),
        ("start_date", [date(2026, 10, 3), date(2026, 10, 1)]),
        (
            "event_time",
            [
                datetime(2026, 10, 3, 12, tzinfo=timezone.utc),
                datetime(2026, 10, 3, 11, tzinfo=timezone.utc),
            ],
        ),
        (
            "created_at",
            [datetime(2026, 10, 3, 12), datetime(2026, 10, 3, 11)],
        ),
        (
            "modified_at",
            [
                "2026-10-03T14:00:00+0200",
                "2026-10-03T11:00:00Z",
            ],
        ),
        (
            "effective_on",
            ["2026-10-03", "2026-10-03T00:00:00"],
        ),
    ],
)
def test_iso_and_native_date_rankings_are_accepted(monkeypatch, column, values):
    class ValidLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return {"is_valid": True, "flag_reason": None}

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service", lambda: ValidLLM()
    )
    result = asyncio.run(
        grade_result(
            "Which records are most recent?",
            {
                "columns": [column],
                "results": [{column: value} for value in values],
                "count": len(values),
            },
            ranking={
                "metric": column,
                "column": column,
                "direction": "DESC",
            },
        )
    )
    assert result["is_valid"] is True


def test_numeric_rankings_remain_valid_with_other_date_columns(monkeypatch):
    class ValidLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return {"is_valid": True, "flag_reason": None}

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service", lambda: ValidLLM()
    )
    result = asyncio.run(
        grade_result(
            "Show highest totals.",
            {
                "columns": ["created_at", "total"],
                "results": [
                    {"created_at": "2026-10-03", "total": 12},
                    {"created_at": "2026-10-01", "total": 8},
                ],
                "count": 2,
            },
            ranking={"metric": "total", "column": "total", "direction": "DESC"},
        )
    )
    assert result["is_valid"] is True


def test_date_rankings_remain_valid_with_other_numeric_columns(monkeypatch):
    class ValidLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return {"is_valid": True, "flag_reason": None}

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service", lambda: ValidLLM()
    )
    result = asyncio.run(
        grade_result(
            "Which records are newest?",
            {
                "columns": ["created_at", "booking_count"],
                "results": [
                    {"created_at": "2026-10-03", "booking_count": 10},
                    {"created_at": "2026-10-01", "booking_count": 15},
                ],
                "count": 2,
            },
            ranking={
                "metric": "created_at",
                "column": "created_at",
                "direction": "DESC",
            },
        )
    )
    assert result["is_valid"] is True


@pytest.mark.parametrize(
    ("values", "expected_reason"),
    [
        (["newest", "older"], "non-numeric value"),
        (["2026-99-99", "2026-01-01"], "non-numeric value"),
        ([12, "2026-01-01"], "incompatible value types"),
    ],
)
def test_invalid_or_mixed_rank_metrics_are_rejected(values, expected_reason):
    result = {
        "columns": ["metric"],
        "results": [{"metric": value} for value in values],
    }
    violation = _ranked_result_violation(
        result,
        {"column": "metric", "direction": "DESC"},
    )
    assert expected_reason in violation


def test_null_ranking_values_keep_the_existing_ordering_rule():
    ranking = {"column": "created_at", "direction": "DESC"}
    valid = {
        "columns": ["created_at"],
        "results": [
            {"created_at": "2026-10-03"},
            {"created_at": None},
        ],
    }
    invalid = {
        "columns": ["created_at"],
        "results": [
            {"created_at": None},
            {"created_at": "2026-10-03"},
        ],
    }
    assert _ranked_result_violation(valid, ranking) is None
    assert "not ordered" in _ranked_result_violation(invalid, ranking)


def test_single_best_ranking_accepts_only_cooptimal_ties():
    ranking = {
        "column": "discount",
        "direction": "DESC",
        "limit": 1,
        "ranking_intent": "single_best_with_ties",
        "explicit_limit": False,
    }
    tied = {
        "columns": ["discount"],
        "results": [{"discount": 50}, {"discount": 50.0}],
    }
    not_tied = {
        "columns": ["discount"],
        "results": [{"discount": 50}, {"discount": 49}],
    }

    assert _ranked_result_violation(tied, ranking) is None
    assert "identical optimal" in _ranked_result_violation(not_tied, ranking)


def test_ranked_result_rejects_exceeding_explicit_row_limit():
    violation = _ranked_result_violation(
        {
            "columns": ["metric"],
            "results": [{"metric": 10}, {"metric": 10}],
        },
        {
            "column": "metric",
            "direction": "DESC",
            "limit": 1,
            "ranking_intent": "top_n",
            "explicit_limit": True,
        },
    )

    assert "explicit row limit" in violation


def test_single_best_metadata_exists_without_user_limit():
    ranking = ranked_result_metadata({
        "ranking_intent": "single_best_with_ties",
        "order_by": [{"column": "highest_discount", "direction": "DESC"}],
        "select_items": [{
            "alias": "highest_discount",
            "metric": "discount_percent",
        }],
    })

    assert ranking == {
        "metric": "discount_percent",
        "column": "highest_discount",
        "direction": "DESC",
        "limit": 1,
        "ranking_intent": "single_best_with_ties",
        "explicit_limit": False,
    }


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
