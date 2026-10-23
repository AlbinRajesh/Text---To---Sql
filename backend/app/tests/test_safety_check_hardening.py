import pytest

from app.agent.graph import _build_validated_execution_query
from app.agent.nodes.generate_query import _validate_llm_output
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


def test_personal_projection_is_pruned_and_public_projection_remains():
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

    safe_query = run_safety_check(
        query,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=[],
    )

    assert "`users`.`email`" not in safe_query["sql"]
    assert [item["alias"] for item in safe_query["validated_query"]["select_items"]] == [
        "user_id",
        "user_name",
    ]
    execution_query, result_columns = _build_validated_execution_query(
        query,
        safe_query,
        schema,
        list(schema),
        [],
    )
    assert result_columns == ["user_id", "user_name"]
    assert "email" not in execution_query["sql"]


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

    with pytest.raises(SafetyViolationError, match="All selected columns"):
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
def test_personal_where_predicate_is_allowed_for_anonymous_scalar_count(
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

    safe_query = run_safety_check(
        query,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=[],
    )

    assert "COUNT(*) AS `user_count`" in safe_query["sql"]
    assert "`users`.`email`" in safe_query["sql"]
    assert value in safe_query["params"]


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
