import asyncio
from decimal import Decimal

import pytest

from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _apply_entity_resolution_constraints,
    _coerce_filter_value,
    _enforce_default_address_filter,
    _normalize_booking_month_semantics,
    _sanitize_plan_values,
    _validate_llm_output,
)
from app.agent.nodes import generate_query as gq
from app.agent.nodes.safety_check import (
    SafetyViolationError,
    _build_single_best_with_ties_sql,
    run_safety_check,
)
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


@pytest.mark.parametrize(
    "relationship_claim",
    [
        {
            "constraint_name": "reviewed_bookings_shop_id",
            "table": "shop_details",
            "referenced_table": "bookings",
        },
        {
            "constraint_name": "stale_constraint_name",
            "table": "bookings",
            "referenced_table": "shop_details",
        },
        {
            "table": "shop_details",
            "referenced_table": "bookings",
        },
    ],
)
def test_join_relationship_resolves_reversed_or_unique_endpoint_claim(
    relationship_claim,
):
    plan = _plan(
        "bookings",
        [{
            "table": "shop_details",
            "column": "name",
            "alias": "shop_name",
        }],
        joins=[{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": relationship_claim,
        }],
    )

    validated = _validate_llm_output(
        plan,
        SCHEMA,
        relationships=RELATIONSHIPS,
        question="Which shops have bookings?",
        matched_business_rules=[],
    )
    compiled = run_safety_check(
        validated,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    direct_compiled = run_safety_check(
        plan,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert validated["joins"][0]["relationship"]["constraint_name"] == (
        "reviewed_bookings_shop_id"
    )
    assert (
        "INNER JOIN `shop_details` ON `bookings`.`shop_id` = `shop_details`.`id`"
        in compiled["sql"]
    )
    assert (
        "INNER JOIN `shop_details` ON `bookings`.`shop_id` = `shop_details`.`id`"
        in direct_compiled["sql"]
    )


def test_join_relationship_without_constraint_rejects_ambiguous_endpoints():
    schema = {
        **SCHEMA,
        "bookings": [
            *SCHEMA["bookings"],
            {"column": "assigned_shop_id", "data_type": "bigint", "key": ""},
        ],
    }
    relationships = [
        *RELATIONSHIPS,
        _edge(
            "reviewed_bookings_assigned_shop_id",
            "bookings",
            "shop_details",
            "assigned_shop_id",
        ),
    ]
    plan = _plan(
        "bookings",
        [{
            "table": "shop_details",
            "column": "name",
            "alias": "shop_name",
        }],
        joins=[{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": {
                "table": "bookings",
                "referenced_table": "shop_details",
            },
        }],
    )

    with pytest.raises(QueryGenerationError, match="verified schema catalog"):
        _validate_llm_output(
            plan,
            schema,
            relationships=relationships,
            question="Which shops have bookings?",
            matched_business_rules=[],
        )


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


def test_single_best_with_ties_allows_grouped_count_ranking():
    plan = _plan(
        "reviews",
        [
            {
                "table": "reviews",
                "column": "shop_id",
                "alias": "shop_id",
            },
            {
                "table": "reviews",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": True,
                "alias": "review_count",
            },
        ],
        group_by=[{"table": "reviews", "column": "shop_id"}],
        order_by=[{"column": "review_count", "direction": "DESC"}],
        ranking_intent="single_best_with_ties",
    )

    validated = _validate_llm_output(
        plan,
        SCHEMA,
        relationships=RELATIONSHIPS,
        question="Which shop is most popular by review count?",
        matched_business_rules=[],
    )
    compiled = _validate_and_compile(plan)

    assert validated["ranking_intent"] == "single_best_with_ties"
    assert validated["limit"] is None
    assert "COUNT(DISTINCT `reviews`.`id`) AS `review_count`" in compiled["sql"]
    assert "DENSE_RANK() OVER (ORDER BY `_tie_source`.`review_count` DESC)" in (
        compiled["sql"]
    )


def test_single_best_with_ties_still_rejects_ungrouped_aggregate():
    plan = _plan(
        "bookings",
        [{
            "table": "bookings",
            "column": "shop_id",
            "aggregate": "SUM",
            "alias": "booking_sum",
        }],
        order_by=[{"column": "booking_sum", "direction": "DESC"}],
        ranking_intent="single_best_with_ties",
    )

    with pytest.raises(
        QueryGenerationError,
        match="grouped numeric-aggregate ranking",
    ):
        _validate_llm_output(
            plan,
            SCHEMA,
            relationships=RELATIONSHIPS,
            question="Most popular by total amount",
            matched_business_rules=[],
        )


def test_single_best_with_ties_allows_grouped_avg_and_strips_limit():
    schema = {
        **SCHEMA,
        "reviews": [
            *SCHEMA["reviews"],
            {"column": "rating", "data_type": "decimal", "key": ""},
        ],
    }
    plan = _plan(
        "reviews",
        [{
            "table": "reviews",
            "column": "rating",
            "aggregate": "AVG",
            "alias": "avg_rating",
        }],
        group_by=[{"table": "reviews", "column": "shop_id"}],
        order_by=[{"column": "avg_rating", "direction": "DESC"}],
        ranking_intent="single_best_with_ties",
    )
    plan["limit"] = 1

    validated = _validate_llm_output(
        plan,
        schema,
        relationships=RELATIONSHIPS,
        question="Which shop has the highest average rating?",
        matched_business_rules=[],
    )
    compiled = run_safety_check(
        validated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=RELATIONSHIPS,
    )

    assert validated["ranking_intent"] == "single_best_with_ties"
    assert validated["limit"] is None
    assert "AVG(`reviews`.`rating`) AS `avg_rating`" in compiled["sql"]
    assert "DENSE_RANK() OVER" in compiled["sql"]


def test_good_restaurant_rule_enforces_verified_joins_filters_and_rating(monkeypatch):
    import asyncio

    from app.agent.nodes import generate_query as generate_query_node
    from app.agent.nodes.business_rules import find_relevant_rules

    rule = next(
        item for item in find_relevant_rules("good restaurants")
        if item.get("term") == "restaurant_quirk_and_rating_filter"
    )
    schema = {
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
            {"column": "subcategory_id", "data_type": "bigint", "key": ""},
        ],
        "subcategories": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "subcategoryname", "data_type": "varchar", "key": ""},
        ],
        "shop_address": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
            {"column": "is_default", "data_type": "tinyint", "key": ""},
            {"column": "city", "data_type": "varchar", "key": ""},
        ],
        "reviews": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
            {"column": "rating", "data_type": "decimal", "key": ""},
        ],
    }
    relationships = [
        {
            **_edge(
                "reviewed_shop_details_subcategory_id",
                "shop_details",
                "subcategories",
                "subcategory_id",
            ),
            "cardinality": "many_to_one",
            "verification": "reviewed_manual_query",
        },
        {
            **_edge(
                "reviewed_shop_address_shop_id",
                "shop_address",
                "shop_details",
                "shop_id",
            ),
            "cardinality": "many_to_one",
            "verification": "reviewed_manual_query",
            "single_row_when": {"column": "is_default", "value": 1},
        },
        {
            **_edge(
                "reviewed_reviews_shop_id",
                "reviews",
                "shop_details",
                "shop_id",
            ),
            "cardinality": "many_to_one",
            "verification": "reviewed_manual_query",
        },
    ]
    query = {
        "answerable": True,
        "table": "reviews",
        "joins": [],
        "select_items": [
            {
                "table": "reviews",
                "column": "shop_id",
                "aggregate": None,
                "alias": "shop_id",
            },
            {
                "table": "reviews",
                "column": "rating",
                "aggregate": "AVG",
                "alias": "avg_rating",
            },
        ],
        "where_expression": None,
        "group_by": [{"table": "reviews", "column": "shop_id"}],
        "having_expression": None,
        "count_groups": False,
    }

    constrained = _apply_entity_resolution_constraints(
        query,
        [rule],
        schema,
        relationships,
    )

    assert {constrained["table"], *(join["table"] for join in constrained["joins"])} == {
        "reviews", "shop_details", "subcategories", "shop_address"
    }
    assert {
        join["relationship"]["constraint_name"]
        for join in constrained["joins"]
    } == {
        "reviewed_reviews_shop_id",
        "reviewed_shop_details_subcategory_id",
        "reviewed_shop_address_shop_id",
    }
    where_conditions = constrained["where_expression"]["conditions"]
    assert {
        (leaf["table"], leaf["column"], leaf["operator"], leaf.get("value"))
        for leaf in where_conditions
    } == {
        ("shop_details", "status", "=", 1),
        ("subcategories", "subcategoryname", "=", "Restaurent"),
        ("shop_address", "is_default", "=", 1),
    }
    assert constrained["having_expression"] == {
        "table": "reviews",
        "column": "rating",
        "aggregate": "AVG",
        "operator": ">=",
        "value": 4,
    }
    compiled = run_safety_check(
        constrained,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )
    assert "HAVING AVG(`reviews`.`rating`) >= %s" in compiled["sql"]
    assert "`subcategories`.`subcategoryname` = %s" in compiled["sql"]

    def unexpected_llm_call():
        raise AssertionError("The grounded restaurant rule should bypass the LLM.")

    monkeypatch.setattr(generate_query_node, "get_llm_service", unexpected_llm_call)
    generated = asyncio.run(generate_query_node.generate_query(
        "Are there any good restaurants in Colachel?",
        schema,
        find_relevant_rules("Are there any good restaurants in Colachel?"),
        relationships=relationships,
    ))
    generated_sql = run_safety_check(
        generated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )["sql"]
    assert "AVG(`reviews`.`rating`)" in generated_sql
    assert "`shop_address`.`city` = %s" in generated_sql


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


@pytest.mark.parametrize(
    ("alias", "order_key"),
    [("saving", "savings"), ("avg_discount", "booking_discount_percent")],
)
def test_tie_rank_resolves_plural_aliases_and_metric_order_keys(alias, order_key):
    query = {
        "select_items": [{
            "metric": "savings" if order_key == "savings" else order_key,
            "alias": alias,
        }],
        "order_by": [{"metric": order_key, "direction": "DESC"}],
    }

    sql = _build_single_best_with_ties_sql(
        "SELECT 1 AS source_value",
        query,
        SCHEMA,
        1,
    )

    assert f"DENSE_RANK() OVER (ORDER BY `_tie_source`.`{alias}` DESC)" in sql
    assert f"`_tie_ranked`.`{alias}` IS NOT NULL" in sql


def test_t131_max_savings_rule_builds_a_safe_single_best_offer_query(monkeypatch):
    schema = {
        **SCHEMA,
        "offer": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
            {"column": "offer_heading", "data_type": "varchar", "key": ""},
            {"column": "actual_price", "data_type": "decimal", "key": ""},
            {"column": "offer_price", "data_type": "decimal", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
    }
    relationships = [
        *RELATIONSHIPS,
        _edge("reviewed_offer_shop_id", "offer", "shop_details", "shop_id"),
    ]
    rules = [{
        "term": "max_discount_amount_offer",
        "definition": (
            "Rank active offers by actual_price minus offer_price."
        ),
        "entity_resolution": {
            "canonical_table": "offer",
            "related_entities": [{"table": "shop_details", "required": True}],
        },
    }]

    def unexpected_llm_call():
        raise AssertionError("The maximum-savings rule should bypass the LLM.")

    monkeypatch.setattr(gq, "get_llm_service", unexpected_llm_call)
    generated = asyncio.run(gq.generate_query(
        "Which deal saves me the most money?",
        schema,
        rules,
        relationships=relationships,
    ))
    compiled = run_safety_check(
        generated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert generated["ranking_intent"] == "single_best_with_ties"
    assert generated["select_items"][0]["alias"] == "name"
    assert generated["select_items"][-1]["metric"] == "savings"
    assert generated["select_items"][-1]["alias"] == "saving"
    assert generated["order_by"][-1] == {
        "table": "offer",
        "column": "id",
        "direction": "ASC",
    }
    assert "`offer`.`status` = %s" in compiled["sql"]
    assert "DENSE_RANK() OVER (ORDER BY `_tie_source`.`saving` DESC)" in compiled["sql"]
    assert "`_compiler_best_rank` = 1" in compiled["sql"]


def test_t075_high_review_count_uses_left_join_and_aggregate_thresholds(monkeypatch):
    schema = {
        **SCHEMA,
        "shop_details": [
            *SCHEMA["shop_details"],
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
        "reviews": [
            *SCHEMA["reviews"],
            {"column": "rating", "data_type": "decimal", "key": ""},
        ],
    }
    relationships = [
        *RELATIONSHIPS,
        _edge("reviewed_reviews_shop_id", "reviews", "shop_details", "shop_id"),
    ]
    rules = [{
        "term": "perfect_rating_high_volume_shops",
        "definition": "Require an average rating of at least five and more than ten reviews.",
        "entity_resolution": {
            "canonical_table": "shop_details",
            "related_entities": [{"table": "reviews", "required": True}],
        },
    }]

    def unexpected_llm_call():
        raise AssertionError("The high-review rule should bypass the LLM.")

    monkeypatch.setattr(gq, "get_llm_service", unexpected_llm_call)
    generated = asyncio.run(gq.generate_query(
        "Which shops have a perfect 5-star rating from more than 10 reviews?",
        schema,
        rules,
        relationships=relationships,
    ))
    compiled = run_safety_check(
        generated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert generated["joins"][0]["join_type"] == "LEFT"
    assert generated["group_by"] == [{"table": "shop_details", "column": "id"}]
    assert "AVG(`reviews`.`rating`) >= %s" in compiled["sql"]
    assert "COUNT(`reviews`.`id`) > %s" in compiled["sql"]
    assert 1 in compiled["params"]
    assert 5 in compiled["params"]
    assert 10 in compiled["params"]


def test_booking_discount_rule_builds_bookings_only_average_metric_plan(monkeypatch):
    schema = {
        **SCHEMA,
        "bookings": [
            *SCHEMA["bookings"],
            {"column": "discount", "data_type": "decimal", "key": ""},
            {"column": "actual_amount", "data_type": "decimal", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
    }
    matched_rules = [{
        "term": "overall_average_discount_percentage",
        "definition": (
            "Average discount percentage per booking on bookings only."
        ),
        "entity_resolution": {"canonical_table": "bookings"},
    }]

    def unexpected_llm_call():
        raise AssertionError("The booking discount rule should bypass the LLM.")

    monkeypatch.setattr(gq, "get_llm_service", unexpected_llm_call)
    generated = asyncio.run(gq.generate_query(
        "What percent discount do we give per booking?",
        schema,
        matched_rules,
        relationships=RELATIONSHIPS,
    ))
    assert generated["table"] == "bookings"
    assert generated["joins"] == []

    result = run_safety_check(
        generated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=RELATIONSHIPS,
    )

    assert (
        "ROUND(AVG((`bookings`.`discount` * 100.0 / "
        "NULLIF(`bookings`.`actual_amount`, 0))), 2)"
        in result["sql"]
    )
    assert "FROM `bookings`" in result["sql"]
    assert "`bookings`.`actual_amount` > %s" in result["sql"]
    assert "`bookings`.`status` = %s" in result["sql"]
    assert result["params"] == [Decimal("0"), 1, 201]


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


@pytest.mark.parametrize(
    "reference_value",
    [
        {
            "column_reference": {
                "table": "shop_details",
                "column": "id",
            }
        },
        "shop_details.id",
    ],
)
def test_reviews_shop_id_column_reference_stays_a_where_comparison(reference_value):
    schema = {
        "reviews": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
        ],
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
        ],
    }
    relationships = [
        _edge(
            "reviewed_reviews_shop_id",
            "reviews",
            "shop_details",
            "shop_id",
        )
    ]
    plan = _plan(
        "reviews",
        [{
            "table": "reviews",
            "column": "id",
            "alias": "review_id",
        }],
        joins=[_join(
            "reviewed_reviews_shop_id", "reviews", "shop_details"
        )],
        where_expression={
            "table": "reviews",
            "column": "shop_id",
            "operator": "=",
            "value": reference_value,
        },
    )
    validated = _validate_llm_output(
        plan,
        schema,
        relationships=relationships,
        question="Show reviews matched to their shops.",
        matched_business_rules=[],
    )

    result = run_safety_check(
        validated,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert "`reviews`.`shop_id` = `shop_details`.`id`" in result["sql"]
    assert "shop_details.id" not in result["params"]


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

    plan["count_entity"] = ["users", "id"]
    repaired = _validate_llm_output(
        plan,
        schema,
        relationships=relationships,
        matched_business_rules=[],
    )
    assert repaired["count_entity"] == {"table": "users", "column": "id"}


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
