import pytest

from app.agent.nodes.generate_query import (
    _remove_unrequested_grouped_null_exclusions,
    _validate_llm_output,
)


@pytest.mark.parametrize(
    ("table", "column", "group"),
    [
        ("shop_address", "city", {}),
        ("bookings", "status", {}),
        ("subcategories", "subcategoryname", {}),
        ("bookings", "created_at", {"date_bucket": "month"}),
        ("shop_details", "category_id", {}),
        ("reviews", "rating", {}),
        ("users", "plan_id", {}),
        ("support_ticket", "state", {}),
    ],
)
def test_unrequested_null_exclusions_are_removed_from_grouped_columns(
    table, column, group
):
    predicate = {
        "table": table,
        "column": column,
        "operator": "IS NOT NULL",
    }

    result = _remove_unrequested_grouped_null_exclusions(
        predicate,
        [{"table": table, "column": column, **group}],
        "Show the breakdown.",
    )

    assert result is None


@pytest.mark.parametrize(
    ("question", "column"),
    [
        ("Exclude records without a city.", "city"),
        ("EXCLUDE records WITHOUT a City!!!", "city"),
        ("Show records excluding status.", "status"),
        ("Ignore the subcategory name.", "subcategoryname"),
        ("Skip records with a known created_at.", "created_at"),
        ("Show only those with a category id.", "category_id"),
        ("Show records that have a rating.", "rating"),
        ("Show non-empty plan id values.", "plan_id"),
        ("Show records where state is not null.", "state"),
    ],
)
def test_explicit_value_requirements_preserve_grouped_null_exclusions(
    question, column
):
    predicate = {
        "table": "records",
        "column": column,
        "operator": "IS NOT NULL",
    }

    result = _remove_unrequested_grouped_null_exclusions(
        predicate,
        [{"table": "records", "column": column}],
        question,
    )

    assert result == predicate


def test_empty_string_exclusion_is_removed_and_empty_where_is_none():
    for operator, value in (("!=", ""), ("<>", " "), ("NOT IN", [""])):
        result = _remove_unrequested_grouped_null_exclusions(
            {
                "boolean": "AND",
                "conditions": [
                    {
                        "table": "records",
                        "column": "city",
                        "operator": operator,
                        "value": value,
                    }
                ],
            },
            [{"table": "records", "column": "city"}],
            "Show the breakdown.",
        )

        assert result is None


def test_non_grouped_and_equality_filters_are_kept():
    expression = {
        "boolean": "AND",
        "conditions": [
            {
                "table": "records",
                "column": "city",
                "operator": "IS NOT NULL",
            },
            {
                "table": "records",
                "column": "active",
                "operator": "=",
                "value": 1,
            },
        ],
    }

    result = _remove_unrequested_grouped_null_exclusions(
        expression,
        [{"table": "records", "column": "city"}],
        "Show the breakdown.",
    )

    assert result == expression["conditions"][1]


def test_or_subtree_is_not_changed():
    expression = {
        "boolean": "OR",
        "conditions": [
            {
                "table": "records",
                "column": "city",
                "operator": "IS NOT NULL",
            },
            {
                "table": "records",
                "column": "active",
                "operator": "=",
                "value": 1,
            },
        ],
    }

    assert _remove_unrequested_grouped_null_exclusions(
        expression,
        [{"table": "records", "column": "city"}],
        "Show the breakdown.",
    ) == expression


def test_mixed_and_or_removes_only_top_level_conjunctive_exclusions():
    or_expression = {
        "boolean": "OR",
        "conditions": [
            {
                "table": "records",
                "column": "city",
                "operator": "IS NOT NULL",
            },
            {
                "table": "records",
                "column": "active",
                "operator": "=",
                "value": 1,
            },
        ],
    }
    expression = {
        "boolean": "AND",
        "conditions": [
            or_expression,
            {
                "table": "records",
                "column": "status",
                "operator": "IS NOT NULL",
            },
        ],
    }

    assert _remove_unrequested_grouped_null_exclusions(
        expression,
        [
            {"table": "records", "column": "city"},
            {"table": "records", "column": "status"},
        ],
        "Show the breakdown.",
    ) == or_expression


def test_default_address_and_at_least_one_primary_key_filters_are_kept():
    default_address = {
        "table": "shop_address",
        "column": "is_default",
        "operator": "=",
        "value": 1,
    }
    entity_primary_key = {
        "table": "bookings",
        "column": "id",
        "operator": "IS NOT NULL",
    }

    assert _remove_unrequested_grouped_null_exclusions(
        default_address,
        [{"table": "shop_address", "column": "is_default"}],
        "Show the default address breakdown.",
    ) == default_address
    assert _remove_unrequested_grouped_null_exclusions(
        entity_primary_key,
        [{"table": "bookings", "column": "id"}],
        "Show shops with at least one completed booking.",
        joins=[{"table": "bookings", "join_type": "LEFT"}],
        schema_context={"bookings": [{"column": "id", "key": "PRI"}]},
    ) == entity_primary_key


def test_assigned_plan_filter_is_kept():
    predicate = {
        "table": "users",
        "column": "plan_id",
        "operator": "IS NOT NULL",
    }

    assert _remove_unrequested_grouped_null_exclusions(
        predicate,
        [{"table": "users", "column": "plan_id"}],
        "Show users by plan with an assigned plan.",
    ) == predicate


def test_at_least_one_protection_applies_only_to_the_matched_entity_table():
    predicate = {
        "table": "shop_details",
        "column": "id",
        "operator": "IS NOT NULL",
    }

    assert _remove_unrequested_grouped_null_exclusions(
        predicate,
        [{"table": "shop_details", "column": "id"}],
        "Show shops with at least one completed booking.",
        joins=[
            {"table": "bookings", "join_type": "LEFT"},
            {"table": "reviews", "join_type": "LEFT"},
        ],
        schema_context={
            "bookings": [{"column": "id", "key": "PRI"}],
            "reviews": [{"column": "id", "key": "PRI"}],
            "shop_details": [{"column": "id", "key": "PRI"}],
        },
    ) is None


@pytest.mark.parametrize(
    "quoted_name",
    ["'At Least One Cafe'", '"At Least One Cafe"', "`At Least One Cafe`", "“At Least One Cafe”"],
)
def test_quoted_names_do_not_trigger_at_least_one_primary_key_protection(
    quoted_name,
):
    predicate = {
        "table": "bookings",
        "column": "id",
        "operator": "IS NOT NULL",
    }

    assert _remove_unrequested_grouped_null_exclusions(
        predicate,
        [{"table": "bookings", "column": "id"}],
        f"Show records for a shop named {quoted_name}.",
        joins=[{"table": "bookings", "join_type": "LEFT"}],
        schema_context={"bookings": [{"column": "id", "key": "PRI"}]},
    ) is None


def test_same_named_column_in_another_table_is_not_treated_as_grouped():
    other_table_filter = {
        "table": "reviews",
        "column": "status",
        "operator": "IS NOT NULL",
    }

    assert _remove_unrequested_grouped_null_exclusions(
        other_table_filter,
        [{"table": "bookings", "column": "status"}],
        "Show the breakdown.",
    ) == other_table_filter


def test_multi_column_grouping_removes_both_eligible_predicates():
    expression = {
        "boolean": "AND",
        "conditions": [
            {
                "table": "records",
                "column": "city",
                "operator": "IS NOT NULL",
            },
            {
                "table": "records",
                "column": "status",
                "operator": "IS NOT NULL",
            },
        ],
    }

    assert _remove_unrequested_grouped_null_exclusions(
        expression,
        [
            {"table": "records", "column": "city"},
            {"table": "records", "column": "status"},
        ],
        "Show the breakdown.",
    ) is None


def test_selected_alias_and_date_bucket_can_express_explicit_value_intent():
    predicate = {
        "table": "records",
        "column": "created_at",
        "operator": "IS NOT NULL",
    }

    assert _remove_unrequested_grouped_null_exclusions(
        predicate,
        [{"table": "records", "column": "created_at", "date_bucket": "month"}],
        "Show only those with a location label.",
        select_items=[
            {
                "table": "records",
                "column": "created_at",
                "alias": "location_label",
            }
        ],
    ) == predicate


@pytest.mark.parametrize(
    "question",
    [
        "",
        "   ",
        "Show shops called 'That Cafe' in each city.",
        "Show shops called `At Least One Cafe` grouped by id.",
        "Show Zürich-area records in each city.",
        ("Show all records grouped by city. " * 100)
        + "Do not exclude missing city values.",
    ],
)
def test_edge_case_wording_does_not_create_unrelated_followup_intent(question):
    predicate = {
        "table": "records",
        "column": "city",
        "operator": "IS NOT NULL",
    }

    result = _remove_unrequested_grouped_null_exclusions(
        predicate,
        [{"table": "records", "column": "city"}],
        question,
    )

    if "Do not exclude missing city values" in question:
        assert result == predicate
    else:
        assert result is None


def test_validator_removes_grouped_null_filter_before_sql_plan_is_returned():
    schema = {
        "records": [
            {"column": "id", "key": "PRI", "data_type": "int"},
            {"column": "city", "data_type": "varchar"},
        ]
    }
    plan = {
        "answerable": True,
        "reasoning": "Group records by city.",
        "table": "records",
        "joins": [],
        "select_items": [
            {
                "table": "records",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": False,
                "alias": "record_count",
            }
        ],
        "where_expression": {
            "table": "records",
            "column": "city",
            "operator": "IS NOT NULL",
        },
        "group_by": [{"table": "records", "column": "city"}],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    validated = _validate_llm_output(
        plan,
        schema,
        question="How many records are in each city?",
    )

    assert validated["where_expression"] is None
