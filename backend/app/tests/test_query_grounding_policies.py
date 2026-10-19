import pytest

from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _validate_llm_output,
    _reject_offer_heading_as_shop_type,
)
from app.agent.query_policies import is_safe_per_shop_review_average


def test_offer_heading_does_not_establish_shop_type():
    expression = {
        "table": "offer",
        "column": "offer_heading",
        "operator": "LIKE",
        "value": "%juice%",
    }
    with pytest.raises(QueryGenerationError) as error:
        _reject_offer_heading_as_shop_type(
            expression,
            "any cheap juice places in Nagercoil",
        )
    assert error.value.rule_id == "offer_heading_not_shop_type"


def test_offer_filter_is_allowed_for_explicit_offer_request():
    expression = {
        "boolean": "AND",
        "conditions": [
            {
                "table": "offer",
                "column": "offer_heading",
                "operator": "LIKE",
                "value": "%juice%",
            },
            {
            },
        ],
    }
    _reject_offer_heading_as_shop_type(
        expression,
        "show me the best juice offers",
    )


def test_average_review_policy_allows_default_address_per_shop():
    joins = [
        {
            "table": "shop_details",
            "relationship": {
                "table": "reviews",
                "referenced_table": "shop_details",
                "cardinality": "many_to_one",
            },
        },
        {
            "table": "shop_address",
            "relationship": {
                "table": "shop_address",
                "referenced_table": "shop_details",
                "cardinality": "many_to_one",
            },
        },
    ]
    group_by = [{"table": "shop_details", "column": "id"}]
    where = {
        "boolean": "AND",
        "conditions": [{
            "table": "shop_address",
            "column": "is_default",
            "operator": "=",
            "value": 1,
        }],
    }

    assert is_safe_per_shop_review_average(
        "reviews", "reviews", "rating", group_by, joins, where
    )
    assert not is_safe_per_shop_review_average(
        "reviews", "reviews", "rating", group_by, joins, None
    )
    assert not is_safe_per_shop_review_average(
        "reviews",
        "reviews",
        "rating",
        [{"table": "shop_details", "column": "name"}],
        joins,
        where,
    )


_OFFER_SCHEMA = {
    "offer": [
        {"column": "id", "key": "PRI"},
        {"column": "offer_heading"},
    ],
}


def _offer_heading_plan():
    return {
        "answerable": True,
        "reasoning": "Find shops by offer heading.",
        "table": "offer",
        "joins": [],
        "select_items": [{
            "table": "offer",
            "column": "offer_heading",
            "aggregate": None,
            "alias": "offer_heading",
        }],
        "where_expression": {
            "table": "offer",
            "column": "offer_heading",
            "operator": "LIKE",
            "value": "%pizza%",
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }


def test_validator_rejects_offer_heading_shop_discovery_when_enabled():
    with pytest.raises(QueryGenerationError) as error:
        _validate_llm_output(
            _offer_heading_plan(),
            _OFFER_SCHEMA,
            question="any cheap pizza places in Nagercoil",
            reject_offer_heading_as_shop_type=True,
        )
    assert error.value.rule_id == "offer_heading_not_shop_type"


def test_validator_leaves_offer_heading_alone_when_not_enabled():
    result = _validate_llm_output(
        _offer_heading_plan(),
        _OFFER_SCHEMA,
        question="any cheap pizza places in Nagercoil",
    )
    assert result["where_expression"]["column"] == "offer_heading"
