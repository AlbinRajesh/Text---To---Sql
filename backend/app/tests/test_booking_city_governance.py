import asyncio

import pytest

from app.agent.nodes import generate_query as gq
from app.agent.nodes import schema_grounding
from app.agent.nodes.safety_check import (
    SafetyViolationError,
    run_safety_check,
)


SCHEMA = {
    "bookings": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "city", "data_type": "varchar", "key": ""},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
}


def _plan(*, group_by=None, where_expression=None):
    return {
        "answerable": True,
        "reasoning": "Use booking city data.",
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "bookings",
            "column": "id",
            "aggregate": None,
            "distinct": False,
            "alias": "booking_id",
        }],
        "group_by": group_by or [],
        "where_expression": where_expression,
    }


@pytest.mark.parametrize(
    ("group_by", "where_expression", "message"),
    [
        ([{"table": "bookings", "column": "city"}], None, "GROUP BY"),
        (
            [],
            {
                "table": "bookings",
                "column": "city",
                "operator": "=",
                "value": "Nagercoil",
            },
            "WHERE",
        ),
    ],
)
def test_generator_rejects_booking_city_group_and_filter(
    group_by, where_expression, message
):
    with pytest.raises(gq.QueryGenerationError, match=message) as error:
        gq._validate_llm_output(
            _plan(group_by=group_by, where_expression=where_expression),
            SCHEMA,
            [],
            "Count bookings by city.",
            [],
        )

    assert error.value.rule_id == "booking_city_governance"
    assert "shop_address.city" in str(error.value)
    assert "is_default = 1" in str(error.value)


@pytest.mark.parametrize(
    ("group_by", "where_expression", "message"),
    [
        ([{"table": "bookings", "column": "city"}], None, "GROUP BY"),
        (
            [],
            {
                "table": "bookings",
                "column": "city",
                "operator": "=",
                "value": "Nagercoil",
            },
            "WHERE",
        ),
    ],
)
def test_safety_layer_independently_rejects_booking_city_usage(
    group_by, where_expression, message
):
    with pytest.raises(SafetyViolationError, match=message) as error:
        run_safety_check(
            {
                "table": "bookings",
                "joins": [],
                "select_items": [{
                    "table": "bookings",
                    "column": "id",
                    "aggregate": None,
                    "alias": "booking_id",
                }],
                "group_by": group_by,
                "where_expression": where_expression,
            },
            valid_tables=list(SCHEMA),
            schema_context=SCHEMA,
            relationships=[],
        )

    assert error.value.rule_id == "booking_city_governance"


def test_live_schema_marks_booking_city_as_unvalidated_free_text(monkeypatch):
    async def fake_fetch_all(_sql):
        return [
            ("bookings", "city", "varchar", "YES", ""),
            ("bookings", "id", "bigint", "NO", "PRI"),
        ]

    monkeypatch.setattr(schema_grounding, "fetch_all", fake_fetch_all)
    schema = asyncio.run(schema_grounding.fetch_schema_from_information_schema())
    city = next(
        column for column in schema["bookings"]
        if column["column"] == "city"
    )

    assert city["groupable"] is False
    assert city["filterable"] is False
    assert "unvalidated free text" in city["semantic_note"].casefold()
