from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.date_resolver import (
    DEFAULT_TIMEZONE,
    DateResolutionError,
    resolve_temporal_intents,
)
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _validate_resolved_temporal_constraints,
)


def test_uses_configured_business_timezone_and_includes_today():
    result = resolve_temporal_intents(
        [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "today",
            "unit": "day",
            "count": 1,
        }],
        now=datetime(2026, 10, 21, 20, 0, tzinfo=ZoneInfo("UTC")),
    )

    assert DEFAULT_TIMEZONE == "Asia/Kolkata"
    assert result["date_ranges"] == [{
        "target": "bookings",
        "start": "2026-10-22",
        "end": "2026-10-23",
        "timezone": "Asia/Kolkata",
        "boundary": "[start,end)",
    }]


def test_previous_complete_month_uses_calendar_boundaries():
    result = resolve_temporal_intents(
        [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "previous_complete",
            "unit": "month",
            "count": 1,
        }],
        now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert result["date_ranges"][0]["start"] == "2026-09-01"
    assert result["date_ranges"][0]["end"] == "2026-10-01"


def test_rolling_six_months_runs_through_the_local_current_day():
    result = resolve_temporal_intents(
        [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "rolling",
            "unit": "month",
            "count": 6,
        }],
        now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert result["date_ranges"][0]["start"] == "2026-04-22"
    assert result["date_ranges"][0]["end"] == "2026-10-23"


def test_rolling_six_months_uses_a_fixed_test_anchor_date():
    result = resolve_temporal_intents(
        [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "rolling",
            "unit": "month",
            "count": 6,
        }],
        now=datetime(2026, 10, 23, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert result["date_ranges"][0] == {
        "target": "bookings",
        "start": "2026-04-23",
        "end": "2026-10-24",
        "timezone": "Asia/Kolkata",
        "boundary": "[start,end)",
    }


def test_rolling_month_clamps_to_the_last_day_of_shorter_month():
    result = resolve_temporal_intents(
        [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "rolling",
            "unit": "month",
            "count": 1,
        }],
        now=datetime(2026, 3, 31, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert result["date_ranges"][0]["start"] == "2026-02-28"


def test_current_year_uses_current_calendar_year_boundaries():
    result = resolve_temporal_intents(
        [{
            "kind": "relative_period",
            "target": "bookings",
            "window": "current_year",
            "unit": "year",
            "count": 1,
        }],
        now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert result["date_ranges"][0]["start"] == "2026-01-01"
    assert result["date_ranges"][0]["end"] == "2027-01-01"


def test_current_state_is_not_converted_to_a_date_range():
    result = resolve_temporal_intents(
        [{
            "kind": "current_state",
            "entity": "offers",
            "state": "active",
            "business_rule_term": "active_offer_status",
        }],
        now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert result["date_ranges"] == []
    assert result["current_state_filters"][0]["business_rule_term"] == (
        "active_offer_status"
    )


@pytest.mark.parametrize(
    "intent",
    [
        {"kind": "relative_period", "target": "bookings", "window": "unsupported", "unit": "month", "count": 1},
        {"kind": "relative_period", "target": "bookings", "window": "rolling", "unit": "month", "count": True},
        {"kind": "relative_period", "target": "bookings", "window": "today", "unit": "month", "count": 1},
        {"kind": "relative_period", "target": "bookings", "window": "current_year", "unit": "month", "count": 1},
    ],
)
def test_rejects_invalid_structured_temporal_intents(intent):
    with pytest.raises(DateResolutionError):
        resolve_temporal_intents([intent])


def test_rejects_naive_clock_and_invalid_timezone():
    intent = [{
        "kind": "relative_period",
        "target": "bookings",
        "window": "today",
        "unit": "day",
        "count": 1,
    }]
    with pytest.raises(DateResolutionError, match="timezone-aware"):
        resolve_temporal_intents(intent, now=datetime(2026, 10, 22))
    with pytest.raises(DateResolutionError, match="timezone is invalid"):
        resolve_temporal_intents(intent, timezone_name="Not/A_Timezone")


def test_query_validation_requires_the_resolved_half_open_range():
    constraints = [{
        "target": "bookings",
        "start": "2026-09-01",
        "end": "2026-10-01",
    }]
    query = {
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "bookings",
                    "column": "created_at",
                    "operator": ">=",
                    "value": "2026-09-01",
                },
                {
                    "table": "bookings",
                    "column": "created_at",
                    "operator": "<",
                    "value": "2026-10-01",
                },
            ],
        },
        "select_items": [],
    }

    _validate_resolved_temporal_constraints(query, constraints)

    query["where_expression"]["conditions"].pop()
    with pytest.raises(QueryGenerationError, match="did not apply"):
        _validate_resolved_temporal_constraints(query, constraints)
