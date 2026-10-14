import pytest

from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check
from app.agent.period_aggregation import normalize_period
from app.agent.nodes.generate_query import _validate_llm_output


SCHEMA = {
    "bookings": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "time_slot_date", "data_type": "date"},
        {"column": "final_amount", "data_type": "decimal"},
        {"column": "status", "data_type": "int"},
    ],
}


def _query(period):
    return {
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "bookings",
            "column": "final_amount",
            "aggregate": "SUM",
            "period": period,
            "alias": "revenue_period",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "order_by": [],
    }


def test_conditional_sum_uses_half_open_dates_and_select_parameter_order():
    plan = _query({
        "table": "bookings",
        "column": "time_slot_date",
        "start": "2025-01-01",
        "end": "2026-01-01",
    })
    compiled = run_safety_check(
        plan,
        valid_tables=["bookings"],
        schema_context=SCHEMA,
    )
    assert "`bookings`.`time_slot_date` >= %s" in compiled["sql"]
    assert "`bookings`.`time_slot_date` < %s" in compiled["sql"]
    assert compiled["params"] == ["2025-01-01", "2026-01-01", 201]


def test_period_comparison_compiles_later_minus_earlier_with_ordered_params():
    plan = _query(None)
    plan["select_items"][0].pop("period")
    plan["select_items"][0]["period_comparison"] = {
        "later_period": {
            "table": "bookings",
            "column": "time_slot_date",
            "start": "2026-01-01",
            "end": "2027-01-01",
        },
        "earlier_period": {
            "table": "bookings",
            "column": "time_slot_date",
            "start": "2025-01-01",
            "end": "2026-01-01",
        },
    }
    compiled = run_safety_check(
        plan,
        valid_tables=["bookings"],
        schema_context=SCHEMA,
    )
    assert ") - SUM(CASE WHEN" in compiled["sql"]
    assert compiled["params"] == [
        "2026-01-01", "2027-01-01",
        "2025-01-01", "2026-01-01",
        201,
    ]


def test_generator_validates_period_comparison_without_unbound_distinct():
    plan = {
        "answerable": True,
        "reasoning": "Compare the two revenue periods.",
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "bookings",
            "column": "final_amount",
            "aggregate": "SUM",
            "period_comparison": {
                "later_period": {
                    "table": "bookings",
                    "column": "time_slot_date",
                    "start": "2026-01-01",
                    "end": "2027-01-01",
                },
                "earlier_period": {
                    "table": "bookings",
                    "column": "time_slot_date",
                    "start": "2025-01-01",
                    "end": "2026-01-01",
                },
            },
            "alias": "revenue_change",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        question="By how much did booking revenue change from 2025 to 2026?",
    )
    assert normalized["select_items"][0]["period_comparison"]["later_period"]["start"] == (
        "2026-01-01"
    )


def test_conditional_count_returns_zero_for_no_matching_rows():
    plan = _query({
        "table": "bookings",
        "column": "time_slot_date",
        "start": "2025-01-01",
        "end": "2026-01-01",
    })
    plan["select_items"][0]["column"] = "*"
    plan["select_items"][0]["aggregate"] = "COUNT"
    compiled = run_safety_check(
        plan,
        valid_tables=["bookings"],
        schema_context=SCHEMA,
    )
    assert "COUNT(CASE WHEN" in compiled["sql"]
    assert "THEN 1 ELSE NULL END)" in compiled["sql"]


@pytest.mark.parametrize(
    "period",
    [
        {"table": "bookings", "column": "status", "start": "2025-01-01", "end": "2026-01-01"},
        {"table": "bookings", "column": "missing", "start": "2025-01-01", "end": "2026-01-01"},
        {"table": "bookings", "column": "time_slot_date", "start": "2025-02-01", "end": "2025-01-01"},
        {"table": "bookings", "column": "time_slot_date", "start": "2025-02-30", "end": "2026-01-01"},
        {"table": "unknown", "column": "time_slot_date", "start": "2025-01-01", "end": "2026-01-01"},
    ],
)
def test_invalid_period_plan_is_rejected(period):
    with pytest.raises(SafetyViolationError):
        run_safety_check(
            _query(period),
            valid_tables=["bookings"],
            schema_context=SCHEMA,
        )


def test_period_normalizer_rejects_unknown_or_non_date_columns():
    with pytest.raises(ValueError):
        normalize_period(
            {
                "table": "bookings",
                "column": "status",
                "start": "2025-01-01",
                "end": "2026-01-01",
            },
            SCHEMA,
            ["bookings"],
        )
