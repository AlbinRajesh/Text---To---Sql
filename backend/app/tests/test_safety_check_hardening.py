import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output
from app.agent.nodes.safety_check import (
    SafetyViolationError,
    run_safety_check,
)
from app.agent.plan_contract import (
    validate_dynamic_date_anchor,
    validate_scalar_date_subquery,
)


VIEWS_SCHEMA = {
    "views": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "created_at", "data_type": "datetime"},
    ],
}


def _plan(where_expression):
    return {
        "answerable": True,
        "reasoning": "Use a validated date anchor.",
        "table": "views",
        "joins": [],
        "select_items": [{
            "table": "views",
            "column": "id",
            "aggregate": None,
            "alias": "view_id",
        }],
        "where_expression": where_expression,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }


def test_dynamic_latest_recorded_date_compiles_to_max_date_subquery():
    anchor = {
        "dynamic_anchor": "latest_recorded_date",
        "table": "views",
        "column": "created_at",
    }
    validated = _validate_llm_output(
        _plan({
            "table": "views",
            "column": "created_at",
            "operator": "<=",
            "value": anchor,
        }),
        VIEWS_SCHEMA,
        relationships=[],
        question="Show views up to the latest recorded date.",
        matched_business_rules=[],
    )

    safe_query = run_safety_check(
        validated,
        valid_tables=list(VIEWS_SCHEMA),
        schema_context=VIEWS_SCHEMA,
        relationships=[],
    )

    assert (
        "`views`.`created_at` <= "
        "(SELECT MAX(DATE(`views`.`created_at`)) FROM `views`)"
    ) in safe_query["sql"]
    assert safe_query["params"] == [201]


@pytest.mark.parametrize(
    ("operation", "aggregate"),
    [("max_date", "MAX"), ("min_date", "MIN")],
)
def test_scalar_date_subquery_compiles_with_validated_day_offset(
    operation, aggregate
):
    validated = _validate_llm_output(
        _plan({
            "table": "views",
            "column": "created_at",
            "operator": "<",
            "value": {
                "scalar_subquery": operation,
                "table": "views",
                "column": "created_at",
                "interval_days": 1,
            },
        }),
        VIEWS_SCHEMA,
        relationships=[],
        question="Show records through the latest recorded day.",
        matched_business_rules=[],
    )

    safe_query = run_safety_check(
        validated,
        valid_tables=list(VIEWS_SCHEMA),
        schema_context=VIEWS_SCHEMA,
        relationships=[],
    )

    assert (
        f"`views`.`created_at` < (SELECT {aggregate}(DATE(`views`.`created_at`)) "
        "FROM `views`) + INTERVAL 1 DAY"
    ) in safe_query["sql"]
    assert safe_query["params"] == [201]


@pytest.mark.parametrize(
    "value",
    [
        {"scalar_subquery": "arbitrary_sql", "table": "views", "column": "created_at"},
        {"scalar_subquery": "max_date", "table": "views", "column": "id"},
        {
            "scalar_subquery": "max_date",
            "table": "views",
            "column": "created_at",
            "interval_days": True,
        },
        {
            "scalar_subquery": "max_date",
            "table": "views",
            "column": "created_at",
            "interval_days": 32,
        },
        {
            "scalar_subquery": "max_date",
            "table": "users",
            "column": "created_at",
        },
    ],
)
def test_scalar_date_subquery_rejects_invalid_operations_or_schema(value):
    with pytest.raises(ValueError):
        validate_scalar_date_subquery(
            value,
            {**VIEWS_SCHEMA, "users": [{"column": "created_at", "data_type": "date"}]},
            {"views"},
            target_table="views",
            target_column="created_at",
        )


def test_current_month_date_boundaries_compile_to_bounded_database_clock_range():
    plan = _plan({
        "boolean": "AND",
        "conditions": [
            {
                "table": "views",
                "column": "created_at",
                "operator": ">=",
                "value": {"calendar_boundary": "current_month_start"},
            },
            {
                "table": "views",
                "column": "created_at",
                "operator": "<",
                "value": {"calendar_boundary": "next_month_start"},
            },
        ],
    })
    validated = _validate_llm_output(
        plan,
        VIEWS_SCHEMA,
        relationships=[],
        question="Show views recorded this month.",
        matched_business_rules=[],
    )

    safe_query = run_safety_check(
        validated,
        valid_tables=list(VIEWS_SCHEMA),
        schema_context=VIEWS_SCHEMA,
        relationships=[],
    )

    assert (
        "CURRENT_DATE() - INTERVAL (DAYOFMONTH(CURRENT_DATE()) - 1) DAY"
        in safe_query["sql"]
    )
    assert (
        "(CURRENT_DATE() - INTERVAL (DAYOFMONTH(CURRENT_DATE()) - 1) DAY) "
        "+ INTERVAL 1 MONTH" in safe_query["sql"]
    )
    assert "MAX(" not in safe_query["sql"]
    assert "%Y" not in safe_query["sql"]
    assert safe_query["params"] == [201]


def test_correlated_exclusion_supports_cross_table_latest_event_day():
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar"},
        ],
        "views": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "user_id", "data_type": "bigint"},
            {"column": "created_at", "data_type": "datetime"},
        ],
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "requester_id", "data_type": "bigint"},
            {"column": "time_slot_date", "data_type": "date"},
            {"column": "created_at", "data_type": "datetime"},
        ],
    }
    relationships = [
        {
            "constraint_name": "reviewed_views_user_id",
            "table": "views",
            "referenced_table": "users",
            "cardinality": "many_to_one",
            "verification": "reviewed_manual_query",
            "column_pairs": [
                {"column": "user_id", "referenced_column": "id"}
            ],
        },
        {
            "constraint_name": "reviewed_bookings_requester_id",
            "table": "bookings",
            "referenced_table": "users",
            "cardinality": "many_to_one",
            "verification": "reviewed_manual_query",
            "column_pairs": [
                {"column": "requester_id", "referenced_column": "id"}
            ],
        },
    ]
    latest_booking_day = {
        "dynamic_anchor": "latest_recorded_date",
        "table": "bookings",
        "column": "created_at",
    }
    plan = {
        "answerable": True,
        "reasoning": "Match users' views to the latest booking day and exclude users booking that day.",
        "table": "users",
        "joins": [{
            "table": "views",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_views_user_id",
                "table": "views",
                "referenced_table": "users",
            },
        }],
        "select_items": [{
            "table": "users",
            "column": "name",
            "aggregate": None,
            "alias": "user_name",
        }],
        "where_expression": {
            "table": "views",
            "column": "created_at",
            "operator": "=",
            "value": latest_booking_day,
        },
        "exclude_entities_with_rows_in": [{
            "table": "bookings",
            "relationship": {
                "constraint_name": "reviewed_bookings_requester_id",
                "table": "bookings",
                "referenced_table": "users",
            },
            "where_expression": {
                "table": "bookings",
                "column": "created_at",
                "operator": "=",
                "value": latest_booking_day,
            },
        }],
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "order_by": [],
    }

    validated = _validate_llm_output(
        plan,
        schema,
        relationships,
        question="Which users viewed an offer but did not book on the latest booking day?",
        matched_business_rules=[],
    )
    compiled = run_safety_check(
        validated,
        list(schema),
        schema,
        relationships,
    )

    assert "NOT EXISTS (SELECT 1 FROM `bookings`" in compiled["sql"]
    assert "`bookings`.`requester_id` = `users`.`id`" in compiled["sql"]
    assert "`views`.`created_at` >= (SELECT MAX(DATE(`bookings`.`created_at`))" in compiled["sql"]
    assert "`bookings`.`created_at` >= (SELECT MAX(DATE(`bookings`.`created_at`))" in compiled["sql"]
    assert "`views`.`created_at` < (SELECT MAX(DATE(`bookings`.`created_at`))" in compiled["sql"]
    assert "`bookings`.`created_at` < (SELECT MAX(DATE(`bookings`.`created_at`))" in compiled["sql"]
    assert "`bookings`.`time_slot_date`" not in compiled["sql"]
    assert compiled["sql"].count(
        "(SELECT MAX(DATE(`bookings`.`created_at`)) FROM `bookings`)"
    ) == 4
    assert compiled["params"] == [201]


@pytest.mark.parametrize(
    "date_value",
    [
        {
            "scalar_subquery": "max_date",
            "table": "bookings",
            "column": "created_at",
        },
        "{'scalar_subquery': 'max_date', 'table': 'bookings', 'column': 'created_at'}",
    ],
)
def test_join_conditions_reject_typed_or_serialized_date_expressions(date_value):
    schema = {
        "users": [{"column": "id", "data_type": "bigint", "key": "PRI"}],
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "requester_id", "data_type": "bigint"},
            {"column": "created_at", "data_type": "timestamp"},
        ],
    }
    relationships = [{
        "constraint_name": "reviewed_bookings_requester_id",
        "table": "bookings",
        "referenced_table": "users",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "requester_id", "referenced_column": "id"}
        ],
    }]
    plan = {
        "answerable": True,
        "reasoning": "Join conditions cannot contain date expressions.",
        "table": "users",
        "joins": [{
            "table": "bookings",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_bookings_requester_id",
                "table": "bookings",
                "referenced_table": "users",
            },
            "conditions": [{
                "table": "bookings",
                "column": "created_at",
                "operator": ">=",
                "value": date_value,
            }],
        }],
        "select_items": [{
            "table": "users",
            "column": "id",
            "aggregate": None,
            "alias": "user_id",
        }],
        "where_expression": None,
    }

    with pytest.raises(QueryGenerationError, match="expression"):
        _validate_llm_output(
            plan,
            schema,
            relationships,
            question="Which users booked on the latest day?",
            matched_business_rules=[],
        )


def test_correlated_exclusion_rejects_unverified_relationship():
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
        ],
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "requester_id", "data_type": "bigint"},
        ],
    }
    plan = {
        "answerable": True,
        "reasoning": "Use a verified anti-join.",
        "table": "users",
        "joins": [],
        "select_items": [{
            "table": "users",
            "column": "id",
            "aggregate": None,
            "alias": "user_id",
        }],
        "where_expression": None,
        "exclude_entities_with_rows_in": [{
            "table": "bookings",
            "relationship": {
                "constraint_name": "invented_relationship",
                "table": "bookings",
                "referenced_table": "users",
            },
            "where_expression": None,
        }],
    }

    with pytest.raises(QueryGenerationError, match="verified many-to-one"):
        _validate_llm_output(plan, schema, relationships=[])


@pytest.mark.parametrize(
    ("function", "operator", "value", "sql_fragment", "expected_params"),
    [
        ("HOUR", "BETWEEN", [2, 4], "HOUR(`views`.`created_at`) BETWEEN %s AND %s", [2, 4, 201]),
        ("TIME", "BETWEEN", ["02:00:00", "04:00:00"],
         "TIME(`views`.`created_at`) BETWEEN %s AND %s",
         ["02:00:00", "04:00:00", 201]),
    ],
)
def test_typed_time_function_filters_compile_with_parameterized_bounds(
    function, operator, value, sql_fragment, expected_params
):
    validated = _validate_llm_output(
        _plan({
            "function": function,
            "table": "views",
            "column": "created_at",
            "operator": operator,
            "value": value,
        }),
        VIEWS_SCHEMA,
        relationships=[],
        question="Show views in the requested time of day.",
        matched_business_rules=[],
    )

    safe_query = run_safety_check(
        validated,
        valid_tables=list(VIEWS_SCHEMA),
        schema_context=VIEWS_SCHEMA,
        relationships=[],
    )

    assert sql_fragment in safe_query["sql"]
    assert safe_query["params"] == expected_params


@pytest.mark.parametrize(
    "anchor",
    [
        {"dynamic_anchor": "arbitrary_sql", "table": "views", "column": "created_at"},
        {"dynamic_anchor": "latest_recorded_date", "table": "users", "column": "created_at"},
        {"dynamic_anchor": "latest_recorded_date", "table": "views", "column": "id"},
        {
            "dynamic_anchor": "latest_recorded_date",
            "table": "views",
            "column": "created_at",
            "expression": "SELECT 1",
        },
    ],
)
def test_dynamic_date_anchor_rejects_unknown_or_unsafe_shapes(anchor):
    with pytest.raises(ValueError):
        validate_dynamic_date_anchor(anchor, VIEWS_SCHEMA, {"views"})


def test_personal_projection_is_rejected_and_public_projection_remains():
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar"},
            {"column": "email", "data_type": "varchar"},
        ],
    }
    query = {
        "table": "users",
        "joins": [],
        "select_items": [
            {
                "table": "users",
                "column": "id",
                "aggregate": None,
                "alias": "user_id",
            },
            {
                "table": "users",
                "column": "email",
                "aggregate": None,
                "alias": "email",
            },
            {
                "table": "users",
                "column": "name",
                "aggregate": None,
                "alias": "user_name",
            },
        ],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    with pytest.raises(SafetyViolationError, match="Personal column 'email'"):
        run_safety_check(
            query,
            valid_tables=list(schema),
            schema_context=schema,
            relationships=[],
        )

    public_query = {
        **query,
        "select_items": [
            item for item in query["select_items"] if item["column"] != "email"
        ],
    }
    safe_query = run_safety_check(
        public_query,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=[],
    )
    assert "`users`.`name` AS `user_name`" in safe_query["sql"]
    assert "`users`.`email`" not in safe_query["sql"]


def test_generator_rejects_same_column_comparisons():
    with pytest.raises(QueryGenerationError, match="cannot compare a column to itself"):
        _validate_llm_output(
            _plan({
                "table": "views",
                "column": "created_at",
                "operator": ">=",
                "value": {
                    "column_reference": {
                        "table": "views",
                        "column": "created_at",
                    },
                },
            }),
            VIEWS_SCHEMA,
            relationships=[],
            question="Show records newer than their own date.",
            matched_business_rules=[],
        )


def test_safety_layer_rejects_same_column_comparisons_independently():
    with pytest.raises(SafetyViolationError, match="cannot compare a column to itself"):
        run_safety_check(
            {
                **_plan({
                    "table": "views",
                    "column": "created_at",
                    "operator": ">=",
                    "value": {
                        "column_reference": {
                            "table": "views",
                            "column": "created_at",
                        },
                    },
                }),
            },
            valid_tables=list(VIEWS_SCHEMA),
            schema_context=VIEWS_SCHEMA,
            relationships=[],
        )


@pytest.mark.parametrize(
    ("function", "value"),
    [("HOUR", 24), ("TIME", "25:00:00"), ("NOW", "12:00:00")],
)
def test_generator_rejects_invalid_time_function_filters(function, value):
    with pytest.raises(QueryGenerationError):
        _validate_llm_output(
            _plan({
                "function": function,
                "table": "views",
                "column": "created_at",
                "operator": "=",
                "value": value,
            }),
            VIEWS_SCHEMA,
            relationships=[],
            question="Filter by time.",
            matched_business_rules=[],
        )


def test_grouped_distribution_percentage_uses_filtered_groups_as_denominator():
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "gender", "data_type": "tinyint"},
        ],
    }
    plan = {
        "answerable": True,
        "reasoning": "Group recorded values and calculate their share.",
        "table": "users",
        "joins": [],
        "select_items": [
            {
                "table": "users",
                "column": "gender",
                "aggregate": None,
                "alias": "gender",
            },
            {
                "metric": "group_distribution_percentage",
                "table": "users",
                "column": "gender",
                "aggregate": None,
                "alias": "percentage",
            },
        ],
        "where_expression": {
            "table": "users",
            "column": "gender",
            "operator": "IS NOT NULL",
        },
        "group_by": [{"table": "users", "column": "gender"}],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    validated = _validate_llm_output(
        plan,
        schema,
        relationships=[],
        question="What percentage of users with a recorded gender fall under each gender value?",
        matched_business_rules=[],
    )

    safe_query = run_safety_check(
        validated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=[],
    )

    assert "`users`.`gender` IS NOT NULL" in safe_query["sql"]
    assert (
        "ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 1) "
        "AS `percentage`"
    ) in safe_query["sql"]
    with pytest.raises(SafetyViolationError, match="must exclude NULL"):
        run_safety_check(
            {
                **validated,
                "where_expression": {
                    "boolean": "OR",
                    "conditions": [
                        {
                            "table": "users",
                            "column": "gender",
                            "operator": "IS NOT NULL",
                        },
                        {
                            "table": "users",
                            "column": "id",
                            "operator": "=",
                            "value": 1,
                        },
                    ],
                },
            },
            valid_tables=list(schema),
            schema_context=schema,
            relationships=[],
        )


def test_personal_projection_cannot_be_the_only_selected_data():
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "email", "data_type": "varchar"},
        ],
    }
    query = {
        "table": "users",
        "joins": [],
        "select_items": [{
            "table": "users",
            "column": "email",
            "aggregate": None,
            "alias": "email",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    with pytest.raises(SafetyViolationError, match="Personal column 'email'"):
        run_safety_check(
            query,
            valid_tables=list(schema),
            schema_context=schema,
            relationships=[],
        )


@pytest.mark.parametrize(
    ("operator", "value"),
    [("LIKE", "%admin%"), ("=", "admin@example.com")],
)
def test_personal_where_predicate_is_rejected_even_for_anonymous_scalar_count(
    operator, value
):
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "email", "data_type": "varchar"},
        ],
    }
    query = {
        "table": "users",
        "joins": [],
        "select_items": [{
            "table": "users",
            "column": "*",
            "aggregate": "COUNT",
            "distinct": False,
            "alias": "user_count",
        }],
        "where_expression": {
            "table": "users",
            "column": "email",
            "operator": operator,
            "value": value,
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    with pytest.raises(SafetyViolationError, match="Personal column 'email'"):
        run_safety_check(
            query,
            valid_tables=list(schema),
            schema_context=schema,
            relationships=[],
        )


@pytest.mark.parametrize(
    ("group_by", "order_by"),
    [
        ([{"table": "users", "column": "email"}], []),
        ([], [{"table": "users", "column": "email", "direction": "ASC"}]),
    ],
)
def test_personal_filter_is_rejected_when_personal_column_is_grouped_or_ordered(
    group_by, order_by
):
    schema = {
        "users": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "email", "data_type": "varchar"},
        ],
    }
    query = {
        "table": "users",
        "joins": [],
        "select_items": [{
            "table": "users",
            "column": "*",
            "aggregate": "COUNT",
            "distinct": False,
            "alias": "user_count",
        }],
        "where_expression": {
            "table": "users",
            "column": "email",
            "operator": "LIKE",
            "value": "%admin%",
        },
        "group_by": group_by,
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": order_by,
    }

    with pytest.raises(SafetyViolationError):
        run_safety_check(
            query,
            valid_tables=list(schema),
            schema_context=schema,
            relationships=[],
        )
