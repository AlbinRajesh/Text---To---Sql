import pytest

from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _coerce_filter_value,
    _enforce_default_address_filter,
    _normalize_booking_month_semantics,
    _sanitize_plan_values,
    _validate_llm_output,
)
from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check
from app.agent.plan_contract import MAX_QUERY_TABLES


SCHEMA = {
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "body", "data_type": "text", "key": ""},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
    ],
    "bookings": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "requester_id", "data_type": "bigint", "key": ""},
        {"column": "final_amount", "data_type": "decimal", "key": ""},
        {"column": "due_date", "data_type": "datetime", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
        {"column": "target_amount", "data_type": "decimal", "key": ""},
    ],
    "users": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
        {"column": "created_at", "data_type": "datetime", "key": ""},
    ],
    "shop_address": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "city", "data_type": "varchar", "key": ""},
        {"column": "is_default", "data_type": "tinyint", "key": ""},
    ],
}


def _edge(name, table, parent, column):
    return {
        "constraint_name": name,
        "table": table,
        "referenced_table": parent,
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{
            "column": column,
            "referenced_column": "id",
        }],
    }


RELATIONSHIPS = [
    _edge("reviewed_bookings_shop_id", "bookings", "shop_details", "shop_id"),
    _edge("reviewed_bookings_requester_id", "bookings", "users", "requester_id"),
]


def _plan(
    table,
    select_items,
    *,
    joins=(),
    where_expression=None,
    group_by=(),
    having_expression=None,
    order_by=(),
    count_entity=None,
    ranking_intent="none",
):
    return {
        "answerable": True,
        "reasoning": "Use verified schema columns and relationships.",
        "table": table,
        "joins": list(joins),
        "select_items": select_items,
        "where_expression": where_expression,
        "group_by": list(group_by),
        "having_expression": having_expression,
        "count_groups": False,
        "count_entity": count_entity,
        "order_by": list(order_by),
        "ranking_intent": ranking_intent,
    }


def _join(name, table, parent):
    return {
        "table": parent,
        "join_type": "INNER",
        "relationship": {
            "constraint_name": name,
            "table": table,
            "referenced_table": parent,
        },
    }


def _validate_and_compile(plan):
    validated = _validate_llm_output(
        plan,
        SCHEMA,
        relationships=RELATIONSHIPS,
        question="Validate this analytical query.",
        matched_business_rules=[],
    )
    return run_safety_check(
        validated,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )


def test_monthly_financial_grouping_uses_transaction_created_at():
    schema = {
        **SCHEMA,
        "bookings": [
            *SCHEMA["bookings"],
            {"column": "created_at", "data_type": "datetime", "key": ""},
            {"column": "time_slot_date", "data_type": "datetime", "key": ""},
            {"column": "time_slot_month", "data_type": "varchar", "key": ""},
        ],
    }
    select_items = [{
        "table": "bookings",
        "column": "time_slot_month",
        "aggregate": None,
        "alias": "booking_month",
    }, {
        "table": "bookings",
        "column": "final_amount",
        "aggregate": "SUM",
        "alias": "monthly_revenue",
    }]
    group_by = [{
        "table": "bookings",
        "column": "time_slot_month",
    }]
    where_expression = {
        "table": "bookings",
        "column": "time_slot_date",
        "operator": ">=",
        "value": "2026-01-01",
    }

    normalized_where = _normalize_booking_month_semantics(
        "monthly revenue by month",
        {"bookings"},
        select_items,
        group_by,
        where_expression,
        schema,
    )

    assert group_by == [{
        "table": "bookings",
        "column": "created_at",
        "date_bucket": "month",
    }]
    assert select_items[0]["column"] == "created_at"
    assert select_items[0]["date_bucket"] == "month"
    assert normalized_where["column"] == "created_at"


def test_explicit_appointment_date_keeps_booking_slot_grouping():
    schema = {
        **SCHEMA,
        "bookings": [
            *SCHEMA["bookings"],
            {"column": "created_at", "data_type": "datetime", "key": ""},
            {"column": "time_slot_date", "data_type": "datetime", "key": ""},
            {"column": "time_slot_month", "data_type": "varchar", "key": ""},
        ],
    }
    group_by = [{"table": "bookings", "column": "created_at"}]

    _normalize_booking_month_semantics(
        "monthly revenue for appointment dates scheduled for each month",
        {"bookings"},
        [],
        group_by,
        None,
        schema,
    )

    assert group_by == [{"table": "bookings", "column": "time_slot_month"}]


def test_upward_many_to_one_offer_join_allows_parent_attributes_and_aggregates():
    schema = {
        **SCHEMA,
        "bookings": [
            *SCHEMA["bookings"],
            {"column": "offer_id", "data_type": "bigint", "key": ""},
        ],
        "offer": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "discount_percent", "data_type": "decimal", "key": ""},
        ],
    }
    relationships = [
        *RELATIONSHIPS,
        _edge("reviewed_bookings_offer_id", "bookings", "offer", "offer_id"),
    ]
    plan = _plan(
        "bookings",
        [{
            "table": "offer",
            "column": "discount_percent",
            "aggregate": "AVG",
            "distinct": False,
            "alias": "average_offer_discount",
        }],
        joins=[_join("reviewed_bookings_offer_id", "bookings", "offer")],
    )

    validated = _validate_llm_output(
        plan,
        schema,
        relationships=relationships,
        question="Average discount on the offers booked.",
        matched_business_rules=[],
    )
    result = run_safety_check(
        validated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert "AVG(`offer`.`discount_percent`) AS `average_offer_discount`" in result["sql"]


def test_calculated_parent_metric_is_safe_from_transaction_base_grain():
    schema = {
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "offer_id", "data_type": "bigint", "key": ""},
        ],
        "offer": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "actual_price", "data_type": "decimal", "key": ""},
            {"column": "offer_price", "data_type": "decimal", "key": ""},
            {"column": "offer_price_p", "data_type": "decimal", "key": ""},
        ],
    }
    relationships = [
        _edge("reviewed_bookings_offer_id", "bookings", "offer", "offer_id"),
    ]
    plan = _plan(
        "bookings",
        [{
            "metric": "discount_percent",
            "aggregate": "AVG",
            "alias": "average_discount_percent",
        }],
        joins=[_join("reviewed_bookings_offer_id", "bookings", "offer")],
    )

    validated = _validate_llm_output(
        plan,
        schema,
        relationships=relationships,
        question="What is the average discount percentage on booked offers?",
        matched_business_rules=[],
    )
    result = run_safety_check(
        validated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert "AVG(" in result["sql"]
    assert "`offer`.`offer_price`" in result["sql"]


@pytest.mark.parametrize("function", ["CHAR_LENGTH", "LENGTH"])
def test_string_length_ordering_compiles_and_keeps_tied_longest_rows(function):
    plan = _plan(
        "reviews",
        [{
            "table": "reviews",
            "column": "body",
            "alias": "review_text",
        }],
        order_by=[{
            "table": "reviews",
            "column": "body",
            "function": function,
            "direction": "DESC",
        }],
        ranking_intent="single_best_with_ties",
    )

    result = _validate_and_compile(plan)

    assert "DENSE_RANK() OVER (ORDER BY `_tie_source`.`review_text` DESC)" in result["sql"]
    assert "`_compiler_best_rank` = 1" in result["sql"]


def test_single_best_with_ties_uses_dense_rank_over_selected_extremum():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "final_amount",
            "alias": "booking_amount",
        }],
        order_by=[{"column": "booking_amount", "direction": "ASC"}],
        ranking_intent="single_best_with_ties",
    )

    result = _validate_and_compile(plan)

    assert "DENSE_RANK() OVER (ORDER BY `_tie_source`.`booking_amount` ASC)" in (
        result["sql"]
    )
    assert "`_tie_ranked`.`_compiler_best_rank` = 1" in result["sql"]
    assert result["sql"].endswith("LIMIT %s")
    assert result["params"][-1] == 1001


def test_single_best_with_ties_boolean_alias_compiles_to_dense_rank():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "final_amount",
            "alias": "booking_amount",
        }],
        order_by=[{"column": "booking_amount", "direction": "ASC"}],
    )
    plan["single_best_with_ties"] = True

    result = _validate_and_compile(plan)

    assert "DENSE_RANK() OVER" in result["sql"]
    assert "`_compiler_best_rank` = 1" in result["sql"]


@pytest.mark.parametrize(
    ("ranking_intent", "boolean_alias"),
    [
        ("single_best_with_ties", False),
        ("none", True),
    ],
)
def test_single_best_with_ties_strips_redundant_limit(
    ranking_intent, boolean_alias
):
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "final_amount",
            "alias": "booking_amount",
        }],
        order_by=[{"column": "booking_amount", "direction": "ASC"}],
        ranking_intent=ranking_intent,
    )
    if boolean_alias:
        plan["single_best_with_ties"] = True
    plan["limit"] = 1

    validated = _validate_llm_output(
        plan,
        SCHEMA,
        relationships=RELATIONSHIPS,
        question="Find the cheapest booking.",
        matched_business_rules=[],
    )
    result = run_safety_check(
        validated,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert validated["ranking_intent"] == "single_best_with_ties"
    assert validated["limit"] is None
    assert "`_compiler_best_rank` = 1" in result["sql"]


def test_multi_table_primary_key_count_is_promoted_to_distinct():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "aggregate": "COUNT",
            "alias": "booking_count",
        }],
        joins=[_join(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        )],
        count_entity={"table": "bookings", "column": "id"},
    )

    result = _validate_and_compile(plan)

    assert "COUNT(DISTINCT `bookings`.`id`) AS `booking_count`" in result["sql"]


def test_fk_column_equality_is_compiled_as_join_on_not_scalar_where():
    plan = _plan(
        "bookings",
        [{
            "table": "shop_details",
            "column": "name",
            "alias": "shop_name",
        }],
        where_expression={
            "table": "bookings",
            "column": "shop_id",
            "operator": "=",
            "value": {
                "column_reference": {
                    "table": "shop_details",
                    "column": "id",
                }
            },
        },
    )

    result = _validate_and_compile(plan)

    assert "INNER JOIN `shop_details` ON `bookings`.`shop_id` = `shop_details`.`id`" in (
        result["sql"]
    )
    assert "WHERE" not in result["sql"]


def test_numeric_coercion_preserves_column_reference_objects():
    reference = {"table": "shop_details", "column": "target_amount"}

    assert _coerce_filter_value(
        {"column_reference": reference},
        "decimal",
        table="bookings",
        column="final_amount",
    ) == {"column_reference": reference}


def test_seven_table_verified_chain_is_supported():
    schema = {
        f"t{index}": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            *(
                [{"column": f"t{index + 1}_id", "data_type": "int", "key": ""}]
                if index < 6 else []
            ),
        ]
        for index in range(7)
    }
    relationships = [
        {
            "constraint_name": f"t{index}_t{index + 1}",
            "table": f"t{index}",
            "referenced_table": f"t{index + 1}",
            "cardinality": "many_to_one",
            "verification": "reviewed_manual_query",
            "column_pairs": [{
                "column": f"t{index + 1}_id",
                "referenced_column": "id",
            }],
        }
        for index in range(6)
    ]
    joins = [
        {
            "table": f"t{index + 1}",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": f"t{index}_t{index + 1}",
                "table": f"t{index}",
                "referenced_table": f"t{index + 1}",
            },
        }
        for index in range(6)
    ]
    query = {
        "table": "t0",
        "joins": joins,
        "select_items": [{
            "table": "t6",
            "column": "id",
            "aggregate": None,
            "alias": "terminal_id",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "order_by": [],
        "ranking_intent": "none",
    }

    result = run_safety_check(
        query,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert MAX_QUERY_TABLES == 7
    assert result["sql"].count(" JOIN ") == 6


def test_string_length_ordering_rejects_non_string_columns():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "alias": "booking_id",
        }],
        order_by=[{
            "table": "bookings",
            "column": "id",
            "function": "LENGTH",
            "direction": "DESC",
        }],
    )

    with pytest.raises(QueryGenerationError, match="verified string column"):
        _validate_llm_output(
            plan,
            SCHEMA,
            relationships=RELATIONSHIPS,
            matched_business_rules=[],
        )


def test_numeric_column_comparison_compiles_as_column_reference():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "alias": "booking_id",
        }],
        joins=[_join(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        )],
        where_expression={
            "table": "bookings",
            "column": "final_amount",
            "operator": ">",
            "value": {
                "column_ref": {
                    "table": "shop_details",
                    "column": "target_amount",
                }
            },
        },
    )

    result = _validate_and_compile(plan)

    assert (
        "`bookings`.`final_amount` > `shop_details`.`target_amount`"
        in result["sql"]
    )
    assert result["params"] == [201]


def test_column_reference_key_compiles_as_validated_cross_table_comparison():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "alias": "booking_id",
        }],
        joins=[_join(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        )],
        where_expression={
            "table": "bookings",
            "column": "final_amount",
            "operator": ">",
            "value": {
                "column_reference": {
                    "table": "shop_details",
                    "column": "target_amount",
                }
            },
        },
    )

    result = _validate_and_compile(plan)

    assert (
        "`bookings`.`final_amount` > `shop_details`.`target_amount`"
        in result["sql"]
    )
    assert result["params"] == [201]


def test_dotted_column_filter_is_normalized_and_compiled_as_reference():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "alias": "booking_id",
        }],
        joins=[_join(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        )],
        where_expression={
            "table": "bookings",
            "column": "final_amount",
            "operator": ">",
            "value": "shop_details.target_amount",
        },
    )

    result = _validate_and_compile(plan)

    assert (
        "`bookings`.`final_amount` > `shop_details`.`target_amount`"
        in result["sql"]
    )
    assert "shop_details.target_amount" not in result["params"]


def test_joined_primary_key_count_synthesizes_count_entity():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "aggregate": "COUNT",
            "distinct": False,
            "alias": "booking_count",
        }],
        joins=[_join(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        )],
    )

    validated = _validate_llm_output(
        plan,
        SCHEMA,
        relationships=RELATIONSHIPS,
        matched_business_rules=[],
    )

    assert validated["select_items"][0]["distinct"] is True
    assert validated["count_entity"] == {
        "table": "bookings",
        "column": "id",
    }


def test_users_joined_count_entity_uses_safety_check_shape():
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "plan_id", "data_type": "bigint", "key": ""},
        ],
        "plans": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
        ],
    }
    relationships = [_edge(
        "reviewed_users_plan_id", "users", "plans", "plan_id"
    )]
    plan = _plan(
        "users",
        [{
            "table": "users",
            "column": "id",
            "aggregate": "COUNT",
            "distinct": False,
            "alias": "user_count",
        }],
        joins=[_join("reviewed_users_plan_id", "users", "plans")],
    )

    validated = _validate_llm_output(
        plan,
        schema,
        relationships=relationships,
        matched_business_rules=[],
    )

    assert validated["count_entity"] == {"table": "users", "column": "id"}


def test_fk_column_comparison_is_moved_into_join_conditions_and_compiles():
    schema = {
        "reviews": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
        ],
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
        ],
    }
    relationships = [_edge(
        "reviewed_reviews_shop_id",
        "reviews",
        "shop_details",
        "shop_id",
    )]
    plan = _plan(
        "shop_details",
        [{
            "table": "reviews",
            "column": "id",
            "alias": "review_id",
        }],
        joins=[{
            "table": "reviews",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_reviews_shop_id",
                "table": "reviews",
                "referenced_table": "shop_details",
            },
        }],
        where_expression={
            "table": "reviews",
            "column": "shop_id",
            "operator": "=",
            "value": "shop_details.id",
        },
    )

    validated = _validate_llm_output(
        plan,
        schema,
        relationships=relationships,
        matched_business_rules=[],
    )
    result = run_safety_check(
        validated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert validated["where_expression"] is None
    assert validated["joins"][0]["conditions"] == [{
        "table": "reviews",
        "column": "shop_id",
        "operator": "=",
        "value": {
            "column_reference": {
                "table": "shop_details",
                "column": "id",
            }
        },
    }]
    assert (
        "`reviews`.`shop_id` = `shop_details`.`id`"
        in result["sql"]
    )
    assert result["params"] == [201]


def test_join_condition_promotes_only_schema_qualified_active_columns():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "alias": "booking_id",
        }],
        joins=[{
            **_join("reviewed_bookings_shop_id", "bookings", "shop_details"),
            "conditions": [
                {
                    "table": "shop_details",
                    "column": "id",
                    "operator": "=",
                    "value": "bookings.shop_id",
                },
                {
                    "table": "shop_details",
                    "column": "name",
                    "operator": "LIKE",
                    "value": "example.com",
                },
            ],
        }],
    )

    validated = _validate_llm_output(
        plan,
        SCHEMA,
        relationships=RELATIONSHIPS,
        matched_business_rules=[],
    )
    conditions = validated["joins"][0]["conditions"]
    assert conditions[0]["value"] == {
        "column_reference": {
            "table": "bookings",
            "column": "shop_id",
        }
    }
    assert conditions[1]["value"] == "example.com"

    result = run_safety_check(
        validated,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    assert "`shop_details`.`id` = `bookings`.`shop_id`" in result["sql"]
    assert "`shop_details`.`name` LIKE %s" in result["sql"]
    assert "example.com" in result["params"]


def test_plan_sanitizer_preserves_join_column_reference_objects():
    plan = {
        "table": "bookings",
        "joins": [{
            "conditions": [{
                "table": "shop_details",
                "column": "id",
                "operator": "=",
                "value": {
                    "column_reference": {
                        "table": "bookings",
                        "column": "shop_id",
                    }
                },
            }]
        }],
        "where_expression": {
            "table": "bookings",
            "column": "shop_id",
            "operator": "=",
            "value": {
                "column_reference": {
                    "table": "shop_details",
                    "column": "id",
                }
            },
        },
    }

    sanitized = _sanitize_plan_values(plan)

    assert sanitized["joins"][0]["conditions"][0]["value"] == {
        "column_reference": {
            "table": "bookings",
            "column": "shop_id",
        }
    }
    assert sanitized["where_expression"]["value"] == "shop_details.id"


def test_column_comparison_rejects_incompatible_types():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "alias": "booking_id",
        }],
        joins=[_join(
            "reviewed_bookings_shop_id", "bookings", "shop_details"
        )],
        where_expression={
            "table": "bookings",
            "column": "final_amount",
            "operator": ">",
            "value": {
                "column_ref": {
                    "table": "shop_details",
                    "column": "name",
                }
            },
        },
    )

    with pytest.raises(QueryGenerationError, match="compatible numeric"):
        _validate_llm_output(
            plan,
            SCHEMA,
            relationships=RELATIONSHIPS,
            matched_business_rules=[],
        )


def test_datetime_column_comparison_compiles_without_literal_binding():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "alias": "booking_id",
        }],
        joins=[_join(
            "reviewed_bookings_requester_id", "bookings", "users"
        )],
        where_expression={
            "table": "bookings",
            "column": "due_date",
            "operator": ">",
            "value": {
                "column_ref": {
                    "table": "users",
                    "column": "created_at",
                }
            },
        },
    )

    result = _validate_and_compile(plan)

    assert "`bookings`.`due_date` > `users`.`created_at`" in result["sql"]
    assert result["params"] == [201]


def test_having_count_supports_forked_many_to_one_parents():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "id",
            "aggregate": "COUNT",
            "distinct": True,
            "alias": "booking_count",
        }],
        joins=[
            _join("reviewed_bookings_shop_id", "bookings", "shop_details"),
            _join("reviewed_bookings_requester_id", "bookings", "users"),
        ],
        group_by=[
            {"table": "shop_details", "column": "name"},
            {"table": "users", "column": "name"},
        ],
        having_expression={
            "table": "bookings",
            "column": "id",
            "aggregate": "COUNT",
            "operator": ">",
            "value": 1,
        },
        count_entity={"table": "bookings", "column": "id"},
    )

    result = _validate_and_compile(plan)

    assert "GROUP BY `shop_details`.`name`, `users`.`name`" in result["sql"]
    assert "HAVING COUNT(`bookings`.`id`) > %s" in result["sql"]


def test_address_count_and_multiple_address_inspection_skip_default_scope():
    address_count = [{
        "table": "shop_address",
        "column": "id",
        "aggregate": "COUNT",
        "distinct": False,
    }]
    _enforce_default_address_filter(
        None,
        {"shop_address"},
        SCHEMA,
        "How many addresses does each shop have?",
        address_count,
    )
    _enforce_default_address_filter(
        None,
        {"shop_address"},
        SCHEMA,
        "Compare multiple shop addresses.",
    )
    _enforce_default_address_filter(
        None,
        {"shop_address"},
        SCHEMA,
        "List shop addresses.",
    )


def test_alternate_address_scope_removes_default_predicates():
    where_expression = {
        "boolean": "AND",
        "conditions": [
            {
                "table": "shop_address",
                "column": "is_default",
                "operator": "=",
                "value": 1,
            },
            {
                "table": "shop_address",
                "column": "city",
                "operator": "=",
                "value": "Nagercoil",
            },
        ],
    }
    joins = [{
        "table": "shop_address",
        "conditions": [{
            "table": "shop_address",
            "column": "is_default",
            "operator": "=",
            "value": 1,
        }],
    }]

    filtered_where = _enforce_default_address_filter(
        where_expression,
        {"shop_address"},
        SCHEMA,
        "Count addresses in all shop addresses.",
        [{"table": "shop_address", "column": "id", "aggregate": "COUNT"}],
        joins=joins,
    )

    assert filtered_where == {
        "table": "shop_address",
        "column": "city",
        "operator": "=",
        "value": "Nagercoil",
    }
    assert joins[0]["conditions"] == []


def test_having_count_over_multiple_address_rows_skips_default_scope():
    _enforce_default_address_filter(
        None,
        {"shop_address"},
        SCHEMA,
        "Find shops with multiple addresses.",
        having_expression={
            "table": "shop_address",
            "column": "id",
            "aggregate": "COUNT",
            "operator": ">",
            "value": 1,
        },
    )
