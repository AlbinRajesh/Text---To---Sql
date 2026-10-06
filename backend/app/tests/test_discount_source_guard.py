"""A discount question must not silently use another table's discount column."""

import pytest

from app.agent.nodes.generate_query import (
    UnanswerableQuestionError,
    _validate_discount_source,
)

SCHEMA = {
    "shop_details": [{"column": "id"}, {"column": "name"}],
    "bookings": [{"column": "id"}, {"column": "shop_id"}, {"column": "discount"}],
    "offer": [{"column": "id"}, {"column": "actual_price"}, {"column": "offer_price"}],
}


def _query(*tables):
    return {
        "table": tables[0],
        "joins": [{"table": table} for table in tables[1:]],
    }


def test_booking_discount_used_for_an_offer_style_question_is_refused():
    with pytest.raises(UnanswerableQuestionError):
        _validate_discount_source(
            _query("shop_details", "bookings"),
            "shops offering a discount of more than 500 rupees",
            SCHEMA,
        )


def test_question_that_names_bookings_is_allowed():
    _validate_discount_source(
        _query("bookings"), "average discount on bookings", SCHEMA
    )


def test_plan_using_the_offer_table_is_allowed():
    _validate_discount_source(
        _query("shop_details", "offer"), "shops with a discount", SCHEMA
    )


def test_question_without_discount_is_ignored():
    _validate_discount_source(
        _query("shop_details", "bookings"), "list shops with bookings", SCHEMA
    )