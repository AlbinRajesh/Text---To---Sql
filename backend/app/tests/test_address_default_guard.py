import pytest

from app.agent.nodes import generate_query as gq
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _enforce_default_address_filter,
)

SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "arearoadname", "data_type": "varchar", "key": ""},
        {"column": "pincode", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
}
SCHEMA_NO_DEFAULT = {"shop_address": SCHEMA["shop_address"][:2]}
DEFAULT_TERM = {
    "table": "shop_address", "column": "is_default", "operator": "=", "value": 1,
}
SHOP_COUNT = [{
    "table": "shop_details",
    "column": "id",
    "aggregate": "COUNT",
    "distinct": True,
}]


def test_shop_address_without_default_filter_is_allowed_for_any_address():
    _enforce_default_address_filter(
        None,
        {"shop_address"},
        SCHEMA,
        "How many shops are there?",
        SHOP_COUNT,
    )


@pytest.mark.parametrize(
    ("question", "where", "group_by"),
    [
        (
            "How many active shops are in each city?",
            None,
            [{"table": "shop_address", "column": "city"}],
        ),
        ("Count shops per city.", None, [{"table": "shop_address", "column": "city"}]),
        (
            "Show the number of shops by location.",
            None,
            [{"table": "shop_address", "column": "arearoadname"}],
        ),
        (
            "Number of active shops in each city.",
            None,
            [{"table": "shop_address", "column": "city"}],
        ),
        (
            "How many active shops are in Nagercoil?",
            {"table": "shop_address", "column": "city", "operator": "=", "value": "Nagercoil"},
            [],
        ),
        (
            "Count shops in this area.",
            {"table": "shop_address", "column": "arearoadname", "operator": "=", "value": "Central Road"},
            [],
        ),
        (
            "Show shops per area.",
            None,
            [{"table": "shop_address", "column": "arearoadname"}],
        ),
        (
            "Count shops by postal code.",
            None,
            [{"table": "shop_address", "column": "pincode"}],
        ),
    ],
)
def test_location_scoped_shop_counts_require_default_address(
    question, where, group_by
):
    with pytest.raises(QueryGenerationError) as info:
        _enforce_default_address_filter(
            where,
            {"shop_details", "shop_address"},
            SCHEMA,
            question,
            SHOP_COUNT,
            group_by=group_by,
        )
    assert info.value.rule_id == "address_requires_default"


@pytest.mark.parametrize(
    "question",
    [
        "Count shops in this city using all addresses.",
        "Count shops in this city using any address.",
        "Count shops by delivery address.",
    ],
)
def test_explicit_alternate_address_scope_does_not_force_default_filter(question):
    _enforce_default_address_filter(
        None,
        {"shop_details", "shop_address"},
        SCHEMA,
        question,
        SHOP_COUNT,
        group_by=[{"table": "shop_address", "column": "city"}],
    )


def test_non_shop_grouping_is_unaffected_by_shop_location_rule():
    _enforce_default_address_filter(
        None,
        {"bookings"},
        SCHEMA,
        "Count bookings by city.",
        group_by=[{"table": "bookings", "column": "city"}],
    )


def test_explicit_default_request_without_filter_is_rejected():
    with pytest.raises(QueryGenerationError) as info:
        _enforce_default_address_filter(
            None, {"shop_address"}, SCHEMA, "Find shops at their default address."
        )
    assert info.value.rule_id == "address_requires_default"


def test_default_request_without_is_default_column_is_rejected():
    with pytest.raises(QueryGenerationError) as info:
        _enforce_default_address_filter(
            None,
            {"shop_address"},
            SCHEMA_NO_DEFAULT,
            "Find shops at their default address.",
        )
    assert info.value.rule_id == "address_default_column_missing"


def test_query_without_shop_address_is_untouched():
    _enforce_default_address_filter(
        None, {"shop_details"}, SCHEMA, "Find shops at their default address."
    )


def test_shop_address_alone_with_filter_passes(monkeypatch):
    # The shape of where_expression is not what is tested here, only the rule,
    # so the term reader is replaced with one that returns the default filter.
    monkeypatch.setattr(gq, "_and_terms", lambda where: [DEFAULT_TERM])
    _enforce_default_address_filter(
        {"any": "plan"},
        {"shop_address"},
        SCHEMA,
        "Find shops at their default address.",
    )