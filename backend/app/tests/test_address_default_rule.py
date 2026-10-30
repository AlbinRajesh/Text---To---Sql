"""Address filters are required only when a default/main address is requested."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output
from app.agent.nodes.safety_check import run_safety_check

SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
}
RELATIONSHIPS = [{
    "constraint_name": "reviewed_shop_address_shop_id",
    "table": "shop_address",
    "referenced_table": "shop_details",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
}]
DEFAULT = {
    "table": "shop_address", "column": "is_default", "operator": "=", "value": 1,
}
OTHER = {
    "table": "shop_address", "column": "id", "operator": ">", "value": 0,
}


def _plan(where):
    return {
        "answerable": True,
        "reasoning": "t",
        "table": "shop_details",
        "joins": [{
            "table": "shop_address",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_shop_address_shop_id",
                "table": "shop_address",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [{
            "table": "shop_details", "column": "id",
            "aggregate": "COUNT", "distinct": True, "alias": "shop_count",
        }],
        "where_expression": where,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": "shop_details", "column": "id"},
        "order_by": [],
    }


def test_unscoped_distinct_count_automatically_uses_default_address():
    normalized = _validate_llm_output(_plan(None), SCHEMA, RELATIONSHIPS)
    assert DEFAULT in normalized["joins"][0]["conditions"]


def test_location_count_automatically_uses_default_address():
    location = {
        "table": "shop_address",
        "column": "city",
        "operator": "=",
        "value": "Nagercoil",
    }
    question = "How many shops are in Nagercoil?"
    normalized = _validate_llm_output(
        _plan(location), SCHEMA, RELATIONSHIPS, question=question
    )
    assert DEFAULT in normalized["joins"][0]["conditions"]
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    assert "COUNT(DISTINCT `shop_details`.`id`)" in built["sql"]
    assert "`shop_address`.`is_default` = %s" in built["sql"]

    explicit = _validate_llm_output(
        _plan({"boolean": "AND", "conditions": [location, DEFAULT]}),
        SCHEMA,
        RELATIONSHIPS,
        question=question,
    )
    built_explicit = run_safety_check(
        explicit,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    assert "COUNT(DISTINCT `shop_details`.`id`)" in built_explicit["sql"]


def test_grouped_location_count_requires_default_address():
    plan = _plan(DEFAULT)
    plan["group_by"] = [{"table": "shop_address", "column": "city"}]
    normalized = _validate_llm_output(
        plan, SCHEMA, RELATIONSHIPS, question="How many active shops are in each city?"
    )
    assert normalized["group_by"] == [
        {"table": "shop_address", "column": "city"}
    ]


def test_unscoped_distinct_count_accepts_default_address_scope():
    normalized = _validate_llm_output(
        _plan(DEFAULT),
        SCHEMA,
        RELATIONSHIPS,
        question="How many shops are there?",
    )
    assert normalized["where_expression"] == DEFAULT


def test_explicit_alternate_address_scope_allows_non_default_shop_count():
    location = {
        "table": "shop_address",
        "column": "city",
        "operator": "=",
        "value": "Nagercoil",
    }
    for question in (
        "Count shops in Nagercoil using all addresses.",
        "Count shops in Nagercoil using any address.",
        "Count shops by delivery address.",
    ):
        assert _validate_llm_output(
            _plan(location), SCHEMA, RELATIONSHIPS, question=question
        )


def test_non_count_address_join_automatically_uses_default_address():
    plan = _plan(None)
    plan["select_items"] = [{
        "table": "shop_details",
        "column": "name",
        "aggregate": None,
        "alias": "shop_name",
    }]
    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        RELATIONSHIPS,
        question="List shops in Nagercoil.",
    )
    assert DEFAULT in normalized["joins"][0]["conditions"]


def test_explicit_default_address_request_injects_filter():
    normalized = _validate_llm_output(
        _plan(None),
        SCHEMA,
        RELATIONSHIPS,
        question="How many shops have a default address in this city?",
    )
    assert DEFAULT in normalized["joins"][0]["conditions"]


def test_default_filter_alone_is_accepted():
    question = "How many shops have a default address in this city?"
    assert _validate_llm_output(
        _plan(DEFAULT), SCHEMA, RELATIONSHIPS, question=question
    )


def test_default_filter_inside_and_group_is_accepted():
    where = {"boolean": "AND", "conditions": [OTHER, DEFAULT]}
    question = "How many shops have a default address in this city?"
    assert _validate_llm_output(
        _plan(where), SCHEMA, RELATIONSHIPS, question=question
    )


def test_default_address_join_scope_is_preserved_with_or_filter():
    where = {"boolean": "OR", "conditions": [DEFAULT, OTHER]}
    question = "How many shops have a default address in this city?"
    normalized = _validate_llm_output(
        _plan(where), SCHEMA, RELATIONSHIPS, question=question
    )
    assert DEFAULT in normalized["joins"][0]["conditions"]


def test_default_filter_in_address_join_conditions_is_accepted():
    plan = _plan(None)
    plan["select_items"] = [{
        "table": "shop_details",
        "column": "name",
        "aggregate": None,
        "alias": "shop_name",
    }]
    plan["joins"][0]["conditions"] = [DEFAULT]
    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        RELATIONSHIPS,
        question="List shops in Nagercoil.",
    )
    assert normalized["joins"][0]["conditions"] == [DEFAULT]
def test_wrong_default_value_is_rejected():
    where = {**DEFAULT, "value": 0}
    question = "How many shops have a default address in this city?"
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(where), SCHEMA, RELATIONSHIPS, question=question
        )
    assert info.value.rule_id == "address_requires_default"


def test_missing_is_default_column_is_rejected():
    schema = {**SCHEMA, "shop_address": SCHEMA["shop_address"][:2]}
    question = "How many shops have a default address in this city?"
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            _plan(None), schema, RELATIONSHIPS, question=question
        )
    assert info.value.rule_id == "address_default_column_missing"


@pytest.mark.parametrize(
    ("city", "aliases"),
    [
        ("Trivandrum", ["Trivandrum", "Thiruvananthapuram"]),
        ("Kochi", ["Kochi", "Cochin"]),
        ("Kanyakumari", ["Kanyakumari", "Kanniyakumari"]),
    ],
)
def test_city_alias_filters_expand_to_parameterized_in(city, aliases):
    location = {
        "table": "shop_address",
        "column": "city",
        "operator": "=",
        "value": city,
    }
    normalized = _validate_llm_output(
        _plan(location),
        SCHEMA,
        RELATIONSHIPS,
        question=f"List shops in {city}.",
    )
    assert normalized["where_expression"]["operator"] == "IN"
    assert normalized["where_expression"]["value"] == aliases
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    assert "`shop_address`.`city` IN (%s, %s)" in built["sql"]
    assert all(alias in built["params"] for alias in aliases)


def test_t111_city_count_includes_aliases_active_filter_and_default_address():
    schema = {
        **SCHEMA,
        "shop_details": [
            *SCHEMA["shop_details"],
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
    }
    question = "how many active shops are there in Kanyakumari?"
    location = {
        "table": "shop_address",
        "column": "city",
        "operator": "=",
        "value": "Kanyakumari",
    }

    normalized = _validate_llm_output(
        _plan(location),
        schema,
        RELATIONSHIPS,
        question=question,
        enforce_active_shop_status=True,
    )
    compiled = run_safety_check(
        normalized,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=RELATIONSHIPS,
    )

    assert normalized["where_expression"]["conditions"] == [
        {
            "table": "shop_address",
            "column": "city",
            "operator": "IN",
            "value": ["Kanyakumari", "Kanniyakumari"],
        },
        {
            "table": "shop_details",
            "column": "status",
            "operator": "=",
            "value": 1,
        },
    ]
    assert DEFAULT in normalized["joins"][0]["conditions"]
    assert "COUNT(DISTINCT `shop_details`.`id`)" in compiled["sql"]
    assert "`shop_address`.`city` IN (%s, %s)" in compiled["sql"]
    assert "`shop_details`.`status` = %s" in compiled["sql"]
    assert "`shop_address`.`is_default` = %s" in compiled["sql"]
    assert "Kanyakumari" in compiled["params"]
    assert "Kanniyakumari" in compiled["params"]
