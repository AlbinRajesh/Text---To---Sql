import pytest

from app.agent.nodes.generate_query import (
    UnanswerableQuestionError,
    _validate_discount_not_price_substitute,
)


def _plan(where=None, select_items=None):
    return {
        "table": "shop_details",
        "joins": [],
        "where_expression": where,
        "having_expression": None,
        "select_items": select_items or [],
    }


def test_case2_actual_price_filter_is_refused():
    plan = _plan(
        where={
            "boolean": "AND",
            "conditions": [
                {
                    "table": "shop_address",
                    "column": "state",
                    "operator": "=",
                    "value": "Tamil Nadu",
                },
                {
                    "table": "offer",
                    "column": "actual_price",
                    "operator": ">",
                    "value": 500,
                },
            ],
        }
    )
    with pytest.raises(UnanswerableQuestionError):
        _validate_discount_not_price_substitute(
            plan, "shops with a discount greater than 500 rupees"
        )


def test_total_discount_on_offer_price_aggregate_is_refused():
    plan = _plan(
        select_items=[
            {"table": "offer", "column": "offer_price", "aggregate": "SUM"}
        ]
    )
    with pytest.raises(UnanswerableQuestionError):
        _validate_discount_not_price_substitute(
            plan, "total discount value by category"
        )


def test_discounted_price_question_is_allowed():
    plan = _plan(
        select_items=[
            {"table": "offer", "column": "offer_price", "aggregate": "AVG"}
        ]
    )
    _validate_discount_not_price_substitute(
        plan, "average discounted offer price for shops named Juice"
    )


def test_question_without_discount_is_allowed():
    plan = _plan(
        where={
            "table": "offer",
            "column": "actual_price",
            "operator": ">",
            "value": 500,
        }
    )
    _validate_discount_not_price_substitute(
        plan, "offers priced above 500"
    )
