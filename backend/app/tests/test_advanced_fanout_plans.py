"""The generator fan-out guard blocks unsafe advanced benchmark shapes."""

import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output
from app.agent.nodes.safety_check import run_safety_check


SCHEMA = {
    "bookings": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "final_amount", "data_type": "decimal", "key": ""},
    ],
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "rating", "data_type": "decimal", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
        {"column": "category_id", "data_type": "bigint", "key": ""},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
    "categories": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "categoriesname", "data_type": "varchar", "key": ""},
    ],
    "offer": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "offer_heading", "data_type": "varchar", "key": ""},
    ],
}


def _edge(name, table, referenced_table, column, referenced_column="id"):
    return {
        "constraint_name": name,
        "table": table,
        "referenced_table": referenced_table,
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{
            "column": column,
            "referenced_column": referenced_column,
        }],
    }


RELATIONSHIPS = [
    _edge("reviewed_bookings_shop_id", "bookings", "shop_details", "shop_id"),
    _edge("reviewed_reviews_shop_id", "reviews", "shop_details", "shop_id"),
    _edge("reviewed_shop_address_shop_id", "shop_address", "shop_details", "shop_id"),
    _edge(
        "reviewed_shop_details_category_id",
        "shop_details",
        "categories",
        "category_id",
    ),
    _edge("reviewed_offer_shop_id", "offer", "shop_details", "shop_id"),
]


def _plan(table, joins, select_items, *, group_by=(), where_expression=None, having=None):
    return {
        "answerable": True,
        "reasoning": "t",
        "table": table,
        "joins": joins,
        "select_items": select_items,
        "where_expression": where_expression,
        "group_by": list(group_by),
        "having_expression": having,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }


def _relationship(constraint_name, table, referenced_table):
    return {
        "constraint_name": constraint_name,
        "table": table,
        "referenced_table": referenced_table,
    }


def test_case_3_dual_child_aggregates_are_rejected():
    plan = _plan(
        "bookings",
        [
            {
                "table": "shop_details",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_bookings_shop_id", "bookings", "shop_details"
                ),
            },
            {
                "table": "reviews",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_reviews_shop_id", "reviews", "shop_details"
                ),
            },
        ],
        [
            {
                "table": "bookings",
                "column": "final_amount",
                "aggregate": "SUM",
                "distinct": False,
                "alias": "total_revenue",
            },
            {
                "table": "reviews",
                "column": "rating",
                "aggregate": "AVG",
                "distinct": False,
                "alias": "average_rating",
            },
        ],
    )
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert info.value.rule_id == "fanout_select_aggregate"
    assert (
        "use isolated_child_aggregates instead of a standard join"
        in str(info.value)
    )


def test_isolated_child_aggregates_compile_without_cross_multiplication():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "shop_name",
        }],
    )
    plan["isolated_child_aggregates"] = [
        {
            "table": "bookings",
            "relationship": _relationship(
                "reviewed_bookings_shop_id", "bookings", "shop_details"
            ),
            "select_items": [{
                "column": "final_amount",
                "aggregate": "SUM",
                "alias": "total_revenue",
            }],
            "where_expression": {
                "table": "bookings",
                "column": "final_amount",
                "operator": ">",
                "value": 10,
            },
        },
        {
            "table": "reviews",
            "relationship": _relationship(
                "reviewed_reviews_shop_id", "reviews", "shop_details"
            ),
            "select_items": [{
                "column": "rating",
                "aggregate": "AVG",
                "alias": "average_rating",
            }],
        },
    ]
    plan["order_by"] = [{"column": "total_revenue", "direction": "DESC"}]
    plan["limit"] = 5
    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    compiled = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "LEFT JOIN (SELECT `bookings`.`shop_id` AS `_base_key`" in compiled["sql"]
    assert "SUM(`bookings`.`final_amount`) AS `total_revenue`" in compiled["sql"]
    assert "AVG(`reviews`.`rating`) AS `average_rating`" in compiled["sql"]
    assert "ORDER BY `total_revenue` DESC, `shop_details`.`id` ASC" in compiled["sql"]
    assert compiled["params"] == [10, 6]


@pytest.mark.parametrize(
    "alias_selection",
    [
        {
            "metric": "average_rating",
            "aggregate": None,
            "distinct": False,
            "alias": "average_rating",
        },
        {
            "column": "average_rating",
            "aggregate": None,
            "distinct": False,
            "alias": "average_rating",
        },
    ],
)
def test_isolated_child_metric_alias_is_resolved_in_select_and_order(alias_selection):
    plan = _plan(
        "shop_details",
        [],
        [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "distinct": False,
                "alias": "shop_name",
            },
            alias_selection,
        ],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
    }]
    plan["order_by"] = [{
        "metric": "average_rating",
        "direction": "DESC",
    }]

    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    compiled = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert [item["alias"] for item in normalized["select_items"]] == ["shop_name"]
    assert normalized["order_by"] == [{
        "column": "average_rating",
        "direction": "DESC",
    }]
    assert "AVG(`reviews`.`rating`) AS `average_rating`" in compiled["sql"]
    assert (
        "ORDER BY `average_rating` IS NULL ASC, `average_rating` DESC"
        in compiled["sql"]
    )


def test_undeclared_isolated_metric_alias_is_rejected():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "shop_name",
        }],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
    }]
    plan["order_by"] = [{
        "metric": "average_rating_typo",
        "direction": "DESC",
    }]

    with pytest.raises(QueryGenerationError, match="Unsupported calculated metric"):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])


def test_undeclared_select_metric_is_rejected():
    plan = _plan(
        "shop_details",
        [],
        [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "distinct": False,
                "alias": "shop_name",
            },
            {
                "metric": "average_rating_typo",
                "aggregate": None,
                "alias": "average_rating",
            },
        ],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
    }]

    with pytest.raises(QueryGenerationError, match="Unsupported calculated metric"):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])


def test_isolated_metric_alias_reference_cannot_rename_compiled_output():
    plan = _plan(
        "shop_details",
        [],
        [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "distinct": False,
                "alias": "shop_name",
            },
            {
                "metric": "average_rating",
                "aggregate": None,
                "alias": "rating",
            },
        ],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
    }]

    with pytest.raises(QueryGenerationError, match="declared alias"):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])


def test_isolated_metric_having_null_order_and_sql_snapshot():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "shop_name",
        }],
        having={
            "metric_alias": "average_rating",
            "operator": ">=",
            "value": 4,
        },
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
    }]
    plan["order_by"] = [{"column": "average_rating", "direction": "DESC"}]
    plan["ranking_intent"] = "single_best_with_ties"
    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert "limit" not in normalized
    compiled = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "RANK() OVER (ORDER BY `_tie_source`.`average_rating` IS NULL ASC, " \
        "`_tie_source`.`average_rating` DESC)" in compiled["sql"]
    assert "WHERE `_tie_ranked`.`_compiler_best_rank` = 1" in compiled["sql"]
    assert "`_tie_ranked`.`average_rating` IS NOT NULL" in compiled["sql"]
    assert compiled["params"] == [4, 1001]
    assert compiled["row_limit"] == 1000
    assert compiled["user_limit"] is False


def test_isolated_metric_alias_cannot_shadow_schema_column():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "shop_name",
        }],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "rating",
        }],
    }]

    with pytest.raises(QueryGenerationError, match="canonical column"):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])


def test_isolated_metric_aliases_must_be_unique_across_the_plan():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "total_revenue",
        }],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "bookings",
        "relationship": _relationship(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        ),
        "select_items": [{
            "column": "final_amount",
            "aggregate": "SUM",
            "alias": "total_revenue",
        }],
    }]

    with pytest.raises(QueryGenerationError, match="Duplicate selected alias"):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])


def test_exclusion_compiles_to_parameterized_not_exists():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "shop_name",
        }],
    )
    plan["exclude_entities_with_rows_in"] = [{
        "table": "offer",
        "relationship": _relationship(
            "reviewed_offer_shop_id", "offer", "shop_details"
        ),
        "where_expression": {
            "table": "offer",
            "column": "offer_heading",
            "operator": "=",
            "value": "clearance",
        },
    }]
    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    compiled = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "NOT EXISTS (SELECT 1 FROM `offer`" in compiled["sql"]
    assert "AND `offer`.`offer_heading` = %s" in compiled["sql"]
    assert compiled["params"] == ["clearance", 201]


def test_exclusion_can_correlate_to_an_already_joined_parent():
    plan = _plan(
        "offer",
        [{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": _relationship(
                "reviewed_offer_shop_id", "offer", "shop_details"
            ),
        }],
        [{
            "table": "offer",
            "column": "offer_heading",
            "aggregate": None,
            "distinct": False,
            "alias": "offer_heading",
        }],
    )
    plan["exclude_entities_with_rows_in"] = [{
        "table": "bookings",
        "relationship": _relationship(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        ),
    }]

    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    compiled = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert (
        "NOT EXISTS (SELECT 1 FROM `bookings` "
        "WHERE `bookings`.`shop_id` = `shop_details`.`id`)"
    ) in compiled["sql"]


def test_exclusion_cannot_traverse_a_parent_not_present_in_the_plan():
    plan = _plan(
        "offer",
        [],
        [{
            "table": "offer",
            "column": "offer_heading",
            "aggregate": None,
            "distinct": False,
            "alias": "offer_heading",
        }],
    )
    plan["exclude_entities_with_rows_in"] = [{
        "table": "bookings",
        "relationship": _relationship(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        ),
    }]

    with pytest.raises(QueryGenerationError, match="already joined"):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])


def test_child_metric_and_separate_child_exclusion_compile_together():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "shop_name",
        }],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
    }]
    plan["exclude_entities_with_rows_in"] = [{
        "table": "bookings",
        "relationship": _relationship(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        ),
    }]

    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    compiled = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "LEFT JOIN (SELECT" in compiled["sql"]
    assert "FROM `reviews`" in compiled["sql"]
    assert (
        "NOT EXISTS (SELECT 1 FROM `bookings` "
        "WHERE `bookings`.`shop_id` = `shop_details`.`id`)"
    ) in compiled["sql"]


def test_isolated_child_aggregate_rejects_unverified_relationship():
    plan = _plan(
        "shop_details",
        [],
        [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "shop_name",
        }],
    )
    plan["isolated_child_aggregates"] = [{
        "table": "reviews",
        "relationship": _relationship(
            "unverified_reviews_shop_id", "reviews", "shop_details"
        ),
        "select_items": [{
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
    }]
    with pytest.raises(QueryGenerationError):
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])


import copy

FLAGGED_RELATIONSHIPS = copy.deepcopy(RELATIONSHIPS)
for _edge_item in FLAGGED_RELATIONSHIPS:
    if _edge_item["constraint_name"] == "reviewed_shop_address_shop_id":
        _edge_item["single_row_when"] = {"column": "is_default", "value": 1}


def _review_average_with_address_plan(where_expression):
    return _plan(
        "reviews",
        [
            {
                "table": "shop_details",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_reviews_shop_id", "reviews", "shop_details"
                ),
            },
            {
                "table": "categories",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_shop_details_category_id",
                    "shop_details",
                    "categories",
                ),
            },
            {
                "table": "shop_address",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_shop_address_shop_id",
                    "shop_address",
                    "shop_details",
                ),
            },
        ],
        [
            {"table": "shop_address", "column": "city", "aggregate": None,
             "distinct": False, "alias": "city"},
            {"table": "reviews", "column": "rating", "aggregate": "AVG",
             "distinct": False, "alias": "average_rating"},
        ],
        group_by=[{"table": "shop_address", "column": "city"}],
        where_expression=where_expression,
    )


_DEFAULT_ADDRESS = {
    "table": "shop_address", "column": "is_default", "operator": "=", "value": 1,
}
_CATEGORY = {
    "table": "categories", "column": "categoriesname", "operator": "=",
    "value": "Transportation Services",
}


def test_case_8_review_average_with_flagged_address_child_is_accepted():
    plan = _review_average_with_address_plan(
        {"boolean": "AND", "conditions": [_DEFAULT_ADDRESS, _CATEGORY]}
    )
    result = _validate_llm_output(plan, SCHEMA, FLAGGED_RELATIONSHIPS, "q", [])
    assert result["table"] == "reviews"
    assert [join["table"] for join in result["joins"]] == [
        "shop_details",
        "categories",
        "shop_address",
    ]


def test_case_8_address_child_without_flag_is_still_rejected():
    plan = _review_average_with_address_plan(
        {"boolean": "AND", "conditions": [_DEFAULT_ADDRESS, _CATEGORY]}
    )
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert info.value.rule_id == "fanout_select_aggregate"


def test_default_address_join_condition_makes_numeric_aggregate_fanout_safe():
    plan = _review_average_with_address_plan(None)
    plan["joins"][2]["conditions"] = [_DEFAULT_ADDRESS]

    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        FLAGGED_RELATIONSHIPS,
        "Average review rating by default shop address.",
        [],
    )
    safety_result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=FLAGGED_RELATIONSHIPS,
    )

    assert "shop_address" in safety_result["sql"]


def test_flagged_address_join_with_filter_under_or_is_not_trusted():
    plan = _review_average_with_address_plan(
        {"boolean": "OR", "conditions": [_DEFAULT_ADDRESS, _CATEGORY]}
    )
    with pytest.raises(QueryGenerationError):
        _validate_llm_output(plan, SCHEMA, FLAGGED_RELATIONSHIPS, "q", [])

def test_case_9_review_average_joined_down_from_shop_is_rejected():
    plan = _plan(
        "shop_details",
        [
            {
                "table": "reviews",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_reviews_shop_id", "reviews", "shop_details"
                ),
            },
            {
                "table": "offer",
                "join_type": "INNER",
                "relationship": _relationship(
                    "reviewed_offer_shop_id", "offer", "shop_details"
                ),
            },
        ],
        [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "distinct": False,
                "alias": "shop_name",
            },
            {
                "table": "reviews",
                "column": "rating",
                "aggregate": "AVG",
                "distinct": False,
                "alias": "average_rating",
            },
        ],
        group_by=[
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        where_expression={
            "table": "offer",
            "column": "offer_heading",
            "operator": "=",
            "value": "example",
        },
    )
    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(plan, SCHEMA, RELATIONSHIPS, "q", [])
    assert info.value.rule_id == "fanout_select_aggregate"
