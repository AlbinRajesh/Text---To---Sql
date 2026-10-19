import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.nodes import generate_query as gq
from app.agent.nodes.business_rules import BusinessRulesStore
from app.agent.nodes.calculated_metrics import compile_metric, validate_metric
from app.agent.nodes.result_grader import grade_result
from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check
from app.agent.prompts.generate_query_prompt import SYSTEM_PROMPT
from app.agent.prompts.grader_prompt import (
    build_grader_prompt,
    build_ranked_result_note,
    ranked_result_metadata,
)


OFFER_SCHEMA = {
    "offer": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint"},
        {"column": "offer_heading", "data_type": "varchar"},
        {"column": "actual_price", "data_type": "decimal"},
        {"column": "offer_price", "data_type": "decimal"},
        {"column": "offer_price_p", "data_type": "decimal"},
        {"column": "coupon_price_p", "data_type": "decimal"},
        {"column": "status", "data_type": "tinyint"},
    ],
    "shop_details": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar"},
        {"column": "status", "data_type": "tinyint"},
    ],
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint"},
        {"column": "rating", "data_type": "decimal"},
    ],
}
OFFER_RELATIONSHIPS = [
    {
        "constraint_name": "reviewed_offer_shop_id",
        "table": "offer",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_reviews_shop_id",
        "table": "reviews",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
]

Q1_EXPECTED = [
    ("Samsung s23ulra", 87000),
    ("i phone pro max", 66500),
    ("Tamil houses", 50000),
    ("i phone 14 pro max", 44999),
    ("i phone 12 used", 31000),
    ("Tech Courses", 27000),
    ("vivo x300 pro", 23000),
    ("E-Commerce App", 22000),
    ("one day offer", 16000),
    ("vivo x 200 fe", 15000),
]
Q2_EXPECTED = [
    ("kpn construction", Decimal("50000")),
    ("Reema Mobile Shop", Decimal("29151.10")),
    ("Karka AI & Tech Academy", Decimal("8970")),
    ("Boss App Studio", Decimal("6757")),
    ("angel timbers", Decimal("6000")),
    ("Sijo Honey Test", Decimal("5000")),
    ("River", Decimal("4000")),
    ("Justclickin", Decimal("3500")),
    ("Nagarajan wood works", Decimal("3000")),
    ("Rajan steels", Decimal("2700")),
]


def _q1_plan():
    return {
        "answerable": True,
        "reasoning": "Return offers ranked by their verified savings metric.",
        "table": "offer",
        "joins": [],
        "select_items": [
            {"table": "offer", "column": "offer_heading", "alias": "offer_heading"},
            {"table": "offer", "column": "actual_price", "alias": "actual_price"},
            {"table": "offer", "column": "offer_price", "alias": "offer_price"},
            {"metric": "savings", "alias": "savings"},
        ],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"column": "savings", "direction": "DESC"}],
        "limit": 10,
    }


def _q2_plan():
    return {
        "answerable": True,
        "reasoning": "Average verified offer savings grouped by shop.",
        "table": "offer",
        "joins": [{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_offer_shop_id",
                "table": "offer",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [
            {"table": "shop_details", "column": "name", "alias": "shop_name"},
            {
                "metric": "savings",
                "aggregate": "AVG",
                "alias": "average_savings_per_offer",
            },
        ],
        "where_expression": None,
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{
            "column": "average_savings_per_offer",
            "direction": "DESC",
        }],
        "limit": 10,
    }


def _q5_plan():
    return {
        "answerable": True,
        "reasoning": "Count unique reviews from shops with at least one active offer.",
        "table": "reviews",
        "joins": [
            {
                "table": "shop_details",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "reviewed_reviews_shop_id",
                    "table": "reviews",
                    "referenced_table": "shop_details",
                },
            },
            {
                "table": "offer",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "reviewed_offer_shop_id",
                    "table": "offer",
                    "referenced_table": "shop_details",
                },
            },
        ],
        "select_items": [{
            "table": "reviews",
            "column": "id",
            "aggregate": "COUNT",
            "distinct": True,
            "alias": "total_reviews",
        }],
        "where_expression": {
            "table": "offer",
            "column": "status",
            "operator": "=",
            "value": 1,
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": "reviews", "column": "id"},
        "order_by": [],
        "limit": None,
    }


def _validated(
    plan,
    question,
    schema=OFFER_SCHEMA,
    relationships=(),
    *,
    enforce_active_shop_status=False,
):
    return gq._validate_llm_output(
        plan,
        schema,
        list(relationships),
        question,
        [],
        enforce_active_shop_status=enforce_active_shop_status,
    )


def _run_sqlite(connection, safe_query):
    parameters = tuple(
        float(value) if isinstance(value, Decimal) else value
        for value in safe_query["params"]
    )
    cursor = connection.execute(
        safe_query["sql"].replace("%s", "?"),
        parameters,
    )
    return cursor.fetchall()


def _memory_database():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE offer (id INTEGER PRIMARY KEY, shop_id INTEGER, "
        "offer_heading TEXT, actual_price NUMERIC, offer_price NUMERIC, "
        "offer_price_p REAL, status INTEGER)"
    )
    connection.execute(
        "CREATE TABLE shop_details (id INTEGER PRIMARY KEY, name TEXT, status INTEGER)"
    )
    connection.execute(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY, shop_id INTEGER)"
    )
    return connection


@pytest.mark.parametrize(
    "question",
    [
        "Show offers with the biggest saving",
        "Find offers with the largest price drop",
        "List offers with the greatest savings",
    ],
)
def test_q1_savings_wordings_validate_and_compile(question):
    gq.preflight_refusals(question)
    validated = _validated(_q1_plan(), question)
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    assert "`offer`.`offer_price`" in safe_query["sql"]
    assert "COALESCE" not in safe_query["sql"]
    assert "ORDER BY `savings` DESC" in safe_query["sql"]
    assert safe_query["params"] == [Decimal("0"), Decimal("0"), 11]
    assert any(
        condition.get("metric") == "final_price"
        and condition.get("operator") == ">"
        and condition.get("value") == Decimal("0")
        for condition in validated["where_expression"]["conditions"]
    )


@pytest.mark.parametrize("alias", ["savings", "savings_amount", "discount_amount"])
def test_order_by_registered_metric_resolves_to_selected_alias(alias):
    plan = _q1_plan()
    plan["select_items"][-1]["alias"] = alias
    plan["order_by"] = [{"metric": "savings", "direction": "DESC"}]

    validated = _validated(plan, "Top 10 offers by savings")
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )

    assert validated["order_by"] == [{"column": alias, "direction": "DESC"}]
    assert f"ORDER BY `{alias}` DESC" in safe_query["sql"]


def test_order_by_registered_metric_accepts_existing_column_shape():
    validated = _validated(_q1_plan(), "Top 10 offers by savings")
    assert validated["order_by"] == [{"column": "savings", "direction": "DESC"}]


def test_query_prompt_documents_registered_metric_order_by_shape():
    assert '{"metric": "savings",' in SYSTEM_PROMPT
    assert '"direction": "DESC"} (or "ASC") in order_by' in SYSTEM_PROMPT
    assert "exactly once in select_items" in SYSTEM_PROMPT


def test_failed_metric_validation_logs_temporary_request_diagnostic(
    monkeypatch, caplog
):
    invalid_plan = _q1_plan()
    invalid_plan["table"] = "shop_details"
    invalid_plan["select_items"] = [{"metric": "savings", "alias": "savings"}]

    class RetryLLM:
        responses = [invalid_plan, _q1_plan()]

        async def call_agent_json(self, *args, **kwargs):
            return self.responses.pop(0)

    monkeypatch.setattr(gq, "get_llm_service", lambda: RetryLLM())
    caplog.set_level("WARNING", logger=gq.logger.name)

    result = asyncio.run(
        gq.generate_query(
            "Top offers by savings",
            OFFER_SCHEMA,
            [],
            OFFER_RELATIONSHIPS,
            request_id="metric-validation-request",
        )
    )

    diagnostic_logs = [
        record.message
        for record in caplog.records
        if "TEMP QUERY_GENERATOR validation failure" in record.message
    ]
    assert len(diagnostic_logs) == 1
    assert "request_id=metric-validation-request attempt=1" in diagnostic_logs[0]
    assert '"metric": "savings"' in diagnostic_logs[0]
    assert "Calculated metric 'savings' requires table 'offer'." in diagnostic_logs[0]
    assert result["table"] == "offer"


def test_order_by_unregistered_metric_is_rejected():
    plan = _q1_plan()
    plan["order_by"] = [{"metric": "arbitrary_sql", "direction": "DESC"}]
    with pytest.raises(gq.QueryGenerationError, match="Unsupported calculated metric"):
        _validated(plan, "Top 10 offers by savings")


def test_order_by_rejects_both_metric_and_column():
    plan = _q1_plan()
    plan["order_by"] = [{
        "metric": "savings",
        "column": "savings",
        "direction": "DESC",
    }]
    with pytest.raises(gq.QueryGenerationError, match="both 'metric' and 'column'"):
        _validated(plan, "Top 10 offers by savings")


def test_q2_avg_savings_metric_order_by_resolves_to_aggregate_alias():
    plan = _q2_plan()
    plan["order_by"] = [{"metric": "savings", "direction": "DESC"}]

    validated = _validated(
        plan,
        "Top shops by average savings per offer",
        relationships=OFFER_RELATIONSHIPS,
    )
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )

    assert validated["order_by"] == [{
        "column": "average_savings_per_offer",
        "direction": "DESC",
    }]
    assert "AVG((" in safe_query["sql"]
    assert "ORDER BY `average_savings_per_offer` DESC" in safe_query["sql"]


@pytest.mark.parametrize(
    "question",
    [
        "Top shops by highest average savings per offer",
        "Shops with the greatest average discount per offer",
        "Rank shops by average price gap per offer",
    ],
)
def test_q2_average_savings_wordings_validate_and_compile(question):
    gq.preflight_refusals(question)
    validated = _validated(_q2_plan(), question, relationships=OFFER_RELATIONSHIPS)
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    assert "AVG((" in safe_query["sql"]
    assert "GROUP BY `shop_details`.`id`, `shop_details`.`name`" in safe_query["sql"]
    assert "ORDER BY `average_savings_per_offer` DESC" in safe_query["sql"]
    assert "`shop_details`.`status`" not in safe_query["sql"]
    assert safe_query["params"] == [Decimal("0"), Decimal("0"), 11]


@pytest.mark.parametrize(
    "question",
    [
        "How many total reviews do shops with active offers have?",
        "Count reviews for shops with active-status offers",
        "How many reviews belong to shops whose offer status is 1?",
    ],
)
def test_q5_active_offer_wordings_use_status_and_distinct_reviews(question):
    gq.preflight_refusals(question)
    validated = _validated(
        _q5_plan(), question, relationships=OFFER_RELATIONSHIPS
    )
    gq._validate_active_offer_plan(validated, question, OFFER_SCHEMA)
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    assert "COUNT(DISTINCT `reviews`.`id`)" in safe_query["sql"]
    assert "`offer`.`status` = %s" in safe_query["sql"]
    assert "shop_details`.`status`" not in safe_query["sql"]
    assert safe_query["params"] == [1, 201]


def test_metric_registry_requires_live_numeric_columns_and_rejects_unknown_metrics():
    numeric = validate_metric("savings", OFFER_SCHEMA, {"offer"})
    assert numeric.name == "savings"
    assert numeric.unit == "currency"
    assert validate_metric("discount_percent", OFFER_SCHEMA, {"offer"}).unit == "percentage"
    sql = compile_metric("final_price", OFFER_SCHEMA, {"offer"})
    assert sql == "`offer`.`offer_price`"

    missing_column_schema = {
        "offer": [
            column for column in OFFER_SCHEMA["offer"]
            if column["column"] != "offer_price"
        ]
    }
    with pytest.raises(ValueError, match="offer_price"):
        compile_metric("final_price", missing_column_schema, {"offer"})

    nonnumeric_schema = {
        **OFFER_SCHEMA,
        "offer": [
            {**column, "data_type": "varchar"}
            if column["column"] == "actual_price"
            else column
            for column in OFFER_SCHEMA["offer"]
        ],
    }
    with pytest.raises(ValueError, match="must be numeric"):
        validate_metric("savings", nonnumeric_schema, {"offer"})
    with pytest.raises(ValueError, match="Unsupported calculated metric"):
        validate_metric("arbitrary_sql", OFFER_SCHEMA, {"offer"})
    with pytest.raises(ValueError, match="requires table"):
        validate_metric("savings", OFFER_SCHEMA, {"shop_details"})


def test_share_of_rows_compiles_a_verified_numeric_condition_and_counts_nulls():
    plan = {
        "answerable": True,
        "reasoning": "Calculate the percentage of review rows with rating five.",
        "table": "reviews",
        "joins": [],
        "select_items": [{
            "metric": "share_of_rows",
            "condition": {
                "table": "reviews",
                "column": "rating",
                "operator": "=",
                "value": 5,
            },
            "round_digits": 1,
            "alias": "five_star_percent",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"metric": "share_of_rows", "direction": "DESC"}],
        "limit": None,
    }
    validated = _validated(plan, "What percentage of reviews are 5 stars?")
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    assert "SUM(CASE WHEN `reviews`.`rating` = 5 THEN 1 ELSE 0 END)" in safe_query["sql"]
    assert "NULLIF(COUNT(*), 0)" in safe_query["sql"]
    assert "ORDER BY `five_star_percent` DESC" in safe_query["sql"]

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE reviews (id INTEGER, shop_id INTEGER, rating NUMERIC)")
    connection.executemany(
        "INSERT INTO reviews VALUES (?, ?, ?)",
        [(1, 1, 5), (2, 1, 5), (3, 1, 4), (4, 1, None)],
    )
    assert _run_sqlite(connection, safe_query) == [(50.0,)]
    connection.close()


def _scoped_share_plan(condition, where_expression):
    return {
        "answerable": True,
        "reasoning": "Five-star percentage among reviews created in 2025.",
        "table": "reviews",
        "joins": [],
        "select_items": [{
            "metric": "share_of_rows",
            "condition": condition,
            "round_digits": 1,
            "alias": "five_star_percentage",
        }],
        "where_expression": where_expression,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": None,
    }


def _reviews_scope_schema():
    return {
        "reviews": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "rating", "data_type": "decimal"},
            {"column": "created_at", "data_type": "datetime"},
        ]
    }


def test_scoped_share_of_rows_validates_and_builds_sql():
    reviews_schema = _reviews_scope_schema()
    plan = _scoped_share_plan(
        {"table": "reviews", "column": "rating", "operator": "=", "value": 5},
        {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "reviews",
                    "column": "created_at",
                    "operator": ">=",
                    "value": "2025-01-01",
                },
                {
                    "table": "reviews",
                    "column": "created_at",
                    "operator": "<",
                    "value": "2026-01-01",
                },
            ],
        },
    )
    validated = _validated(
        plan,
        "What percentage of reviews created in 2025 were rated five stars?",
        schema=reviews_schema,
    )
    safe_query = run_safety_check(
        validated, list(reviews_schema), reviews_schema, []
    )

    assert "SUM(CASE WHEN `reviews`.`rating` = 5 THEN 1 ELSE 0 END)" in safe_query["sql"]
    assert "`reviews`.`created_at` >= %s" in safe_query["sql"]
    assert "`reviews`.`created_at` < %s" in safe_query["sql"]
    assert safe_query["params"][:2] == ["2025-01-01", "2026-01-01"]


def test_share_of_rows_defaults_rounding_to_one_decimal():
    reviews_schema = _reviews_scope_schema()
    plan = _scoped_share_plan(
        {"table": "reviews", "column": "rating", "operator": "=", "value": 5},
        None,
    )
    plan["select_items"][0].pop("round_digits")

    validated = _validated(plan, "What percentage of reviews are five stars?", schema=reviews_schema)
    safe_query = run_safety_check(validated, list(reviews_schema), reviews_schema, [])

    assert "ROUND((SUM(CASE WHEN `reviews`.`rating` = 5 THEN 1 ELSE 0 END)" in safe_query["sql"]
    assert "), 1) AS `five_star_percentage`" in safe_query["sql"]


def test_share_of_rows_preserves_explicit_round_digits():
    reviews_schema = _reviews_scope_schema()
    for digits in (2, 0):
        plan = _scoped_share_plan(
            {"table": "reviews", "column": "rating", "operator": "=", "value": 5},
            None,
        )
        plan["select_items"][0]["round_digits"] = digits

        validated = _validated(plan, "What percentage of reviews are five stars?", schema=reviews_schema)
        safe_query = run_safety_check(validated, list(reviews_schema), reviews_schema, [])

        assert f"), {digits}) AS `five_star_percentage`" in safe_query["sql"]


def test_average_rating_without_round_digits_remains_unrounded():
    reviews_schema = _reviews_scope_schema()
    plan = {
        "answerable": True,
        "reasoning": "Calculate the average review rating.",
        "table": "reviews",
        "joins": [],
        "select_items": [{
            "table": "reviews",
            "column": "rating",
            "aggregate": "AVG",
            "alias": "average_rating",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": None,
    }

    validated = _validated(plan, "What is the average review rating?", schema=reviews_schema)
    safe_query = run_safety_check(validated, list(reviews_schema), reviews_schema, [])

    assert "ROUND(" not in safe_query["sql"]


def test_share_of_rows_rejects_date_scope_nested_in_condition():
    plan = _scoped_share_plan(
        {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "reviews",
                    "column": "created_at",
                    "operator": ">=",
                    "value": "2025-01-01",
                },
                {
                    "table": "reviews",
                    "column": "created_at",
                    "operator": "<",
                    "value": "2026-01-01",
                },
                {
                    "table": "reviews",
                    "column": "rating",
                    "operator": "=",
                    "value": 5,
                },
            ],
        },
        None,
    )

    with pytest.raises(
        gq.QueryGenerationError,
        match="share_of_rows supports numeric comparison operators only",
    ):
        _validated(
            plan,
            "What percentage of reviews created in 2025 were rated five stars?",
            schema=_reviews_scope_schema(),
        )


def test_share_of_rows_rejects_unverified_condition():
    plan = {
        "answerable": True,
        "reasoning": "Calculate a share.",
        "table": "reviews",
        "joins": [],
        "select_items": [{
            "metric": "share_of_rows",
            "condition": {
                "table": "reviews",
                "column": "shop_id",
                "operator": "; DROP TABLE reviews; --",
                "value": 1,
            },
            "alias": "share",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": None,
    }
    with pytest.raises(gq.QueryGenerationError, match="comparison operators only"):
        _validated(plan, "What share of reviews are from shop 1?")


def test_share_of_rows_can_compare_a_registered_metric():
    plan = {
        "answerable": True,
        "reasoning": "Calculate the share of offers with discounts above 50 percent.",
        "table": "offer",
        "joins": [],
        "select_items": [{
            "metric": "share_of_rows",
            "condition": {
                "metric": "discount_percent",
                "operator": ">",
                "value": 50,
            },
            "round_digits": 1,
            "alias": "offer_percentage",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": None,
    }
    validated = _validated(
        plan,
        "What percentage of offers have a discount greater than 50 percent?",
    )
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )

    assert "NULLIF(COUNT(*), 0)" in safe_query["sql"]
    assert "offer_price" in safe_query["sql"]
    assert "discount_percent" not in safe_query["sql"]

    connection = _memory_database()
    connection.executemany(
        "INSERT INTO offer VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "A", 100, 40, None, 1),
            (2, 1, "B", 100, 40, None, 1),
            (3, 1, "C", 100, 80, None, 1),
            (4, 1, "D", 100, 100, None, 1),
        ],
    )
    assert _run_sqlite(connection, safe_query) == [(50.0,)]
    connection.close()


def test_conditional_joined_counts_use_one_join_and_preserve_empty_categories():
    schema = {
        "categories": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "categoriesname", "data_type": "varchar"},
        ],
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "category_id", "data_type": "bigint"},
            {"column": "name", "data_type": "varchar"},
            {"column": "status", "data_type": "tinyint"},
        ],
    }
    relationships = [{
        "constraint_name": "reviewed_shop_details_category_id",
        "table": "shop_details",
        "referenced_table": "categories",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{
            "column": "category_id",
            "referenced_column": "id",
        }],
    }]
    plan = {
        "answerable": True,
        "reasoning": "Count active and inactive shops by category.",
        "table": "categories",
        "joins": [{
            "table": "shop_details",
            "join_type": "LEFT",
            "relationship": {
                "constraint_name": "reviewed_shop_details_category_id",
                "table": "shop_details",
                "referenced_table": "categories",
            },
        }],
        "select_items": [
            {"table": "categories", "column": "id", "alias": "category_id"},
            {
                "table": "categories",
                "column": "categoriesname",
                "alias": "category_name",
            },
            {
                "table": "shop_details",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": True,
                "condition": {
                    "table": "shop_details",
                    "column": "status",
                    "operator": "=",
                    "value": 1,
                },
                "alias": "active_shop_count",
            },
            {
                "table": "shop_details",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": True,
                "condition": {
                    "table": "shop_details",
                    "column": "status",
                    "operator": "=",
                    "value": 0,
                },
                "alias": "inactive_shop_count",
            },
        ],
        "where_expression": None,
        "group_by": [
            {"table": "categories", "column": "id"},
            {"table": "categories", "column": "categoriesname"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": "shop_details", "column": "id"},
        "order_by": [],
        "limit": None,
    }
    validated = _validated(
        plan,
        "For each category, compare the number of active shops with the number of inactive shops.",
        schema,
        relationships,
        enforce_active_shop_status=True,
    )
    safe_query = run_safety_check(
        validated,
        list(schema),
        schema,
        relationships,
    )
    assert safe_query["sql"].count("LEFT JOIN `shop_details`") == 1
    assert "COUNT(DISTINCT CASE WHEN `shop_details`.`status` = %s" in safe_query["sql"]
    assert safe_query["params"][:2] == [1, 0]

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE categories (id INTEGER PRIMARY KEY, categoriesname TEXT)"
    )
    connection.execute(
        "CREATE TABLE shop_details (id INTEGER PRIMARY KEY, category_id INTEGER, "
        "name TEXT, status INTEGER)"
    )
    connection.executemany(
        "INSERT INTO categories VALUES (?, ?)",
        [(1, "Active and inactive"), (2, "No shops")],
    )
    connection.executemany(
        "INSERT INTO shop_details VALUES (?, ?, ?, ?)",
        [(1, 1, "A", 1), (2, 1, "B", 0), (3, 1, "C", 1)],
    )
    assert _run_sqlite(connection, safe_query) == [
        (1, "Active and inactive", 2, 1),
        (2, "No shops", 0, 0),
    ]
    connection.close()


def test_retry_cannot_turn_a_validation_failure_into_an_unanswerable_verdict(
    monkeypatch,
):
    class FakeLLM:
        async def call_agent_json(self, *_args, **_kwargs):
            return responses.pop(0)

    responses = [
        {
            "answerable": True,
            "reasoning": "Use the offer table.",
            "table": "unknown_table",
        },
        {
            "answerable": False,
            "reasoning": "The requested comparison is unsupported.",
        },
    ]
    monkeypatch.setattr(gq, "get_llm_service", lambda: FakeLLM())

    with pytest.raises(
        gq.QueryGenerationError,
        match="after a prior plan failed validation",
    ):
        asyncio.run(
            gq.generate_query(
                "Show all offers.",
                OFFER_SCHEMA,
                [],
            )
        )


def test_share_of_rows_can_be_grouped_without_join_fanout():
    plan = {
        "answerable": True,
        "reasoning": "Calculate each shop's five-star review share.",
        "table": "reviews",
        "joins": [{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_reviews_shop_id",
                "table": "reviews",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [
            {"table": "shop_details", "column": "name", "alias": "shop_name"},
            {
                "metric": "share_of_rows",
                "condition": {
                    "table": "reviews",
                    "column": "rating",
                    "operator": "=",
                    "value": 5,
                },
                "round_digits": 1,
                "alias": "five_star_percent",
            },
        ],
        "where_expression": None,
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"metric": "share_of_rows", "direction": "DESC"}],
        "limit": 10,
    }
    validated = _validated(
        plan,
        "What percentage of each shop's reviews are 5 stars?",
        relationships=OFFER_RELATIONSHIPS,
    )
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE reviews (id INTEGER, shop_id INTEGER, rating NUMERIC)")
    connection.execute("CREATE TABLE shop_details (id INTEGER, name TEXT, status INTEGER)")
    connection.executemany(
        "INSERT INTO reviews VALUES (?, ?, ?)",
        [(1, 1, 5), (2, 1, 4), (3, 2, 5)],
    )
    connection.executemany(
        "INSERT INTO shop_details VALUES (?, ?, ?)",
        [(1, "Shop A", 1), (2, "Shop B", 1)],
    )
    assert _run_sqlite(connection, safe_query) == [
        ("Shop B", 100.0),
        ("Shop A", 50.0),
    ]
    connection.close()


def test_percentage_threshold_must_use_percentage_metric():
    plan = _q1_plan()
    plan["where_expression"] = {
        "metric": "savings",
        "operator": ">",
        "value": 50,
    }
    with pytest.raises(gq.QueryGenerationError, match="percentage threshold"):
        _validated(plan, "Which offers have a discount of more than 50%?")


def test_discount_percent_metric_compiles_for_percentage_threshold():
    plan = _q1_plan()
    plan["select_items"][-1] = {
        "metric": "discount_percent",
        "alias": "discount_percent",
    }
    plan["where_expression"] = {
        "metric": "discount_percent",
        "operator": ">",
        "value": 50,
    }
    plan["order_by"] = []
    validated = _validated(plan, "Which offers have a discount of more than 50%?")
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    assert "NULLIF(`offer`.`actual_price`, 0)" in safe_query["sql"]
    assert "`offer`.`offer_price`" in safe_query["sql"]
    assert "`offer`.`offer_price_p`" in safe_query["sql"]
    assert safe_query["params"] == [Decimal("50"), 11]
    assert "CASE WHEN" in safe_query["sql"]
    connection = _memory_database()
    connection.executemany(
        "INSERT INTO offer VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "over threshold", 100, 40, None, 1),
            (2, 2, "below threshold", 100, 60, None, 1),
            (3, 3, "percentage threshold boundary", 100, None, 50, 1),
            (4, 4, "zero original price", 0, 0, None, 1),
            (5, 5, "conflicting percentage", 100, 10, 50, 1),
            (6, 6, "one currency unit", 100, 1, None, 1),
            (7, 7, "percentage-only offer", 100, None, 90, 1),
        ],
    )
    rows = _run_sqlite(connection, safe_query)
    assert {row[0]: row[-1] for row in rows} == {
        "over threshold": 60.0,
        "one currency unit": 99.0,
        "percentage-only offer": 90,
    }
    connection.close()


def test_coupon_discount_percentage_uses_its_separate_registered_metric():
    assert validate_metric(
        "coupon_discount_percent", OFFER_SCHEMA, {"offer"}
    ).unit == "percentage"
    assert "coupon_price_p" in compile_metric(
        "coupon_discount_percent", OFFER_SCHEMA, {"offer"}
    )


def test_safety_layer_rechecks_metric_threshold_units():
    validated = _validated(_q1_plan(), "Top offers by savings")
    validated["metric_filter_unit"] = "percentage"
    with pytest.raises(SafetyViolationError, match="percentage threshold"):
        run_safety_check(
            validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
        )


def test_unsupported_ratio_plan_is_refused_without_a_percentage_metric():
    plan = {
        "answerable": True,
        "reasoning": "Count offers.",
        "table": "offer",
        "joins": [],
        "select_items": [{
            "table": "offer",
            "column": "*",
            "aggregate": "COUNT",
            "alias": "offer_count",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": None,
    }
    with pytest.raises(gq.UnanswerableQuestionError, match="Ratios between separately counted"):
        _validated(plan, "What is the ratio of active to inactive offers?")


def test_calculated_metric_plan_rejects_model_supplied_expression():
    raw_plan = _q1_plan()
    raw_plan["select_items"][-1]["expression"] = "actual_price - offer_price"
    with pytest.raises(gq.QueryGenerationError, match="model-supplied columns or expressions"):
        _validated(raw_plan, "Top offers by savings")

    raw_sql_plan = _q1_plan()
    raw_sql_plan["sql"] = "SELECT 1"
    with pytest.raises(gq.QueryGenerationError, match="Model-supplied SQL"):
        _validated(raw_sql_plan, "Top offers by savings")


def test_q1_expected_ranked_savings_and_edge_cases():
    plan = _validated(_q1_plan(), "Top 10 offers with highest savings")
    safe_query = run_safety_check(
        plan, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    connection = _memory_database()
    rows = []
    for index, (name, savings) in enumerate(Q1_EXPECTED, 1):
        actual_price = savings + 10000
        rows.append(
            (index, index, name, actual_price, 10000, None, 0)
        )
    rows.extend([
        (20, 20, "percent offer", 100, None, 90, 0),
        (21, 21, "all null", None, None, None, 0),
        (22, 22, "zero original", 0, None, 90, 0),
        (23, 23, "price below offer", 50, 100, None, 0),
        (24, 24, "one rupee final", 100, 1, None, 0),
        (25, 25, "null actual", None, 25, None, 0),
    ])
    connection.executemany(
        "INSERT INTO offer VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    results = _run_sqlite(connection, safe_query)
    assert [(row[0], int(row[3])) for row in results[:10]] == Q1_EXPECTED
    included_names = {row[0] for row in results}
    assert not {
        "all null",
        "zero original",
        "price below offer",
        "null actual",
        "percent offer",
    } & included_names
    connection.close()


def test_savings_metric_includes_valid_one_unit_offer_price():
    plan = _q1_plan()
    plan["order_by"] = []
    plan["limit"] = None
    validated = _validated(plan, "Show offers and their savings")
    safe_query = run_safety_check(
        validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    connection = _memory_database()
    connection.execute(
        "INSERT INTO offer VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "one currency unit", 100, 1, None, 1),
    )

    rows = _run_sqlite(connection, safe_query)
    connection.close()

    assert rows == [("one currency unit", 100, 1, 99)]


def test_q2_expected_average_savings_and_ranked_list():
    plan = _validated(
        _q2_plan(),
        "Top 10 shops by highest average savings per offer",
        relationships=OFFER_RELATIONSHIPS,
        enforce_active_shop_status=True,
    )
    safe_query = run_safety_check(
        plan, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    connection = _memory_database()
    shops = [(index, name, 0) for index, (name, _) in enumerate(Q2_EXPECTED, 1)]
    connection.executemany("INSERT INTO shop_details VALUES (?, ?, ?)", shops)
    offers = [
        (index, index, name, float(savings) + 10000, 10000, None, 0)
        for index, (name, savings) in enumerate(Q2_EXPECTED, 1)
    ]
    connection.executemany("INSERT INTO offer VALUES (?, ?, ?, ?, ?, ?, ?)", offers)
    results = _run_sqlite(connection, safe_query)
    assert [row[0] for row in results] == [name for name, _ in Q2_EXPECTED]
    assert [round(Decimal(str(row[1])), 2) for row in results] == [
        round(value, 2) for _, value in Q2_EXPECTED
    ]
    assert "Sijo Honey Test" in [row[0] for row in results]
    connection.close()


def test_q5_active_offer_review_count_is_distinct_and_equals_17():
    question = "How many total reviews do shops with active offers have?"
    plan = _validated(
        _q5_plan(),
        question,
        relationships=OFFER_RELATIONSHIPS,
        enforce_active_shop_status=True,
    )
    gq._validate_active_offer_plan(plan, question, OFFER_SCHEMA)
    safe_query = run_safety_check(
        plan, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS
    )
    connection = _memory_database()
    connection.executemany(
        "INSERT INTO reviews VALUES (?, ?)",
        [(index, 1) for index in range(1, 18)]
        + [(18, 2), (19, 2)],
    )
    connection.executemany(
        "INSERT INTO shop_details VALUES (?, ?, ?)",
        [(1, "active shop", 0), (2, "inactive shop", 0)],
    )
    connection.executemany(
        "INSERT INTO offer VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "active one", 100, 50, None, 1),
            (2, 1, "active two", 100, 50, None, 1),
            (3, 2, "inactive", 100, 50, None, 0),
        ],
    )
    assert _run_sqlite(connection, safe_query) == [(17,)]
    connection.close()


def test_active_offer_policy_rejects_shop_status_or_date_filters():
    plan = _validated(
        _q5_plan(),
        "How many reviews for shops with active offers",
        relationships=OFFER_RELATIONSHIPS,
    )
    plan["where_expression"] = {
        "boolean": "AND",
        "conditions": [
            plan["where_expression"],
            {
                "table": "shop_details",
                "column": "status",
                "operator": "=",
                "value": 1,
            },
        ],
    }
    with pytest.raises(gq.QueryGenerationError, match="shop_details.status"):
        gq._validate_active_offer_plan(
            plan, "How many reviews for shops with active offers", OFFER_SCHEMA
        )


def test_ranked_note_is_built_from_validated_query_plan():
    validated = _validated(
        _q1_plan(), "Top 10 offers with highest savings"
    )
    metadata = ranked_result_metadata(validated)
    assert metadata == {
        "metric": "savings",
        "column": "savings",
        "direction": "DESC",
        "limit": 10,
    }
    note = build_ranked_result_note(metadata)
    prompt = build_grader_prompt("Top 10 offers", "rows", "SELECT ...", note)
    assert "Ranking metric: savings" in prompt
    assert "direction: DESC" in prompt
    assert "limit: 10" in prompt
    assert "Later rows in a correct top-N list may have lower values" in prompt


def test_result_grader_rejects_wrong_order_but_accepts_lower_later_ranks(monkeypatch):
    metadata = {
        "metric": "savings",
        "column": "savings",
        "direction": "DESC",
        "limit": 3,
    }

    class UnexpectedLLM:
        async def call_agent_json(self, *args, **kwargs):
            raise AssertionError("Invalid ordering should fail before the LLM call.")

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service", lambda: UnexpectedLLM()
    )
    wrong = asyncio.run(grade_result(
        "Top savings offers",
        {
            "results": [
                {"savings": 100},
                {"savings": 300},
                {"savings": 200},
            ],
            "columns": ["savings"],
            "count": 3,
        },
        ranking=metadata,
    ))
    assert wrong["is_valid"] is False
    assert "not ordered" in wrong["flag_reason"]

    class ValidLLM:
        async def call_agent_json(self, *args, **kwargs):
            return {"is_valid": True, "flag_reason": None}

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service", lambda: ValidLLM()
    )
    valid = asyncio.run(grade_result(
        "Top savings offers",
        {
            "results": [
                {"savings": 300},
                {"savings": 200},
                {"savings": 100},
            ],
            "columns": ["savings"],
            "count": 3,
        },
        ranking=metadata,
    ))
    assert valid["is_valid"] is True


@pytest.mark.parametrize(
    ("question", "plan_factory", "relationships"),
    [
        ("biggest saving", _q1_plan, ()),
        ("largest price drop", _q1_plan, ()),
        ("greatest savings", _q1_plan, ()),
        ("highest average savings per offer", _q2_plan, OFFER_RELATIONSHIPS),
        ("greatest average discount per offer", _q2_plan, OFFER_RELATIONSHIPS),
        ("average price gap per offer", _q2_plan, OFFER_RELATIONSHIPS),
        ("active offers", _q5_plan, OFFER_RELATIONSHIPS),
        ("active-status offers", _q5_plan, OFFER_RELATIONSHIPS),
        ("offer status is 1", _q5_plan, OFFER_RELATIONSHIPS),
    ],
)
def test_stage1_phrasings_match_rules_and_validate_plans(
    question, plan_factory, relationships
):
    gq.preflight_refusals(question)
    rules_path = Path(__file__).parents[1] / "rules" / "business_rules.json"
    rules_store = BusinessRulesStore(str(rules_path))
    matched_rules = rules_store.find_relevant_rules(question)
    expected_term = (
        "active_offer_status" if plan_factory is _q5_plan else "savings"
    )
    assert expected_term in {rule["term"] for rule in matched_rules}
    plan = _validated(
        plan_factory(), question, relationships=relationships
    )
    if expected_term == "active_offer_status":
        gq._validate_active_offer_plan(plan, question, OFFER_SCHEMA)
    assert plan.get("limit") == (
        None if expected_term == "active_offer_status" else 10
    )


def test_preflight_keeps_relative_time_refusal_with_savings():
    with pytest.raises(gq.UnanswerableQuestionError, match="Relative dates"):
        gq.preflight_refusals("Show offers with the biggest savings right now")


def test_business_rules_remain_valid_json():
    rules_path = Path(__file__).parents[1] / "rules" / "business_rules.json"
    with rules_path.open(encoding="utf-8") as file:
        data = json.load(file)
    assert any(rule["term"] == "active_offer_status" for rule in data["terms"])
