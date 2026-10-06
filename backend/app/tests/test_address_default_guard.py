import pytest

from app.agent.nodes import generate_query as gq
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _enforce_default_address_filter,
)

SCHEMA = {
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
}
SCHEMA_NO_DEFAULT = {"shop_address": SCHEMA["shop_address"][:2]}
DEFAULT_TERM = {
    "table": "shop_address", "column": "is_default", "operator": "=", "value": 1,
}


def test_shop_address_alone_without_filter_is_rejected():
    with pytest.raises(QueryGenerationError) as info:
        _enforce_default_address_filter(None, {"shop_address"}, SCHEMA)
    assert info.value.rule_id == "address_requires_default"


def test_shop_address_alone_missing_column_is_rejected():
    with pytest.raises(QueryGenerationError) as info:
        _enforce_default_address_filter(None, {"shop_address"}, SCHEMA_NO_DEFAULT)
    assert info.value.rule_id == "address_default_column_missing"


def test_query_without_shop_address_is_untouched():
    _enforce_default_address_filter(None, {"shop_details"}, SCHEMA)


def test_shop_address_alone_with_filter_passes(monkeypatch):
    # The shape of where_expression is not what is tested here, only the rule,
    # so the term reader is replaced with one that returns the default filter.
    monkeypatch.setattr(gq, "_and_terms", lambda where: [DEFAULT_TERM])
    _enforce_default_address_filter({"any": "plan"}, {"shop_address"}, SCHEMA)