import asyncio
import sqlite3
from decimal import Decimal

import pytest
from app.agent.nodes.generate_query import (
    _enforce_at_least_one_joined_entity,
    _table_name_forms,
)



from app.agent.nodes.generate_query import (
    QueryGenerationError,
    UnanswerableQuestionError,
    generate_query,
    preflight_refusals,
    _expand_date_only_bounds,
    _normalize_grouped_dimension_count,
    _normalize_filter_expression,
    _validate_llm_output,
)
from app.agent.nodes.business_rules import find_relevant_rules
from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.result_grader import grade_result
from app.agent.nodes.run_self_fix import run_query
from app.agent.nodes.safety_check import (
    SafetyViolationError,
    _ast_verify,
    run_safety_check,
)
from app.api.chat import ChatResponse, _build_error_response, _build_response
from app.agent.nodes.cache_check import check_cache
from app.services.summary_service import generate_summary
from app.tests.query_generation_benchmark import (
    _check_execution_expectation,
    _safe_execution_preview,
)


SCHEMA = {
    "shop_details": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
        {"column": "phone"},
        {"column": "websitelink"},
        {"column": "category_id"},
        {"column": "subcategory_id"},
    ],
    "reviews": [
        {"column": "id", "key": "PRI", "data_type": "bigint"},
        {"column": "shop_id", "data_type": "bigint"},
        {"column": "user_id", "data_type": "bigint"},
        {"column": "rating", "data_type": "float"},
    ],
    "bookings": [
        {"column": "id", "key": "PRI", "data_type": "bigint"},
        {"column": "shop_id", "data_type": "bigint"},
        {"column": "final_amount", "data_type": "decimal"},
    ],
    "offer": [
        {"column": "id", "key": "PRI", "data_type": "bigint"},
        {"column": "shop_id", "data_type": "bigint"},
        {"column": "offer_price", "data_type": "decimal"},
    ],
    "users": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
        {"column": "password"},
    ],
    "plans": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
        {"column": "price"},
        {"column": "status"},
    ],
    "categories": [
        {"column": "id", "key": "PRI"},
        {"column": "categoriesname"},
    ],
    "subcategories": [
        {"column": "id", "key": "PRI"},
        {"column": "category_id"},
        {"column": "subcategoryname"},
    ],
    "regions": [
        {"column": "id", "key": "PRI"},
        {"column": "region_id"},
        {"column": "name"},
    ],
    "countries": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
    ],
    "unrelated": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
    ],
}

RELATIONSHIPS = [
    {
        "constraint_name": "shops_category_fk",
        "table": "shop_details",
        "referenced_table": "categories",
        "cardinality": "many_to_one",
        "column_pairs": [{"column": "category_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "subcategories_category_fk",
        "table": "subcategories",
        "referenced_table": "categories",
        "column_pairs": [{"column": "category_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "regions_subcategory_fk",
        "table": "regions",
        "referenced_table": "subcategories",
        "column_pairs": [{"column": "region_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "countries_region_fk",
        "table": "regions",
        "referenced_table": "countries",
        "column_pairs": [{"column": "region_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_reviews_shop_id",
        "table": "reviews",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_reviews_user_id",
        "table": "reviews",
        "referenced_table": "users",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "user_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_bookings_shop_id",
        "table": "bookings",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_offer_shop_id",
        "table": "offer",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
]


def _category_count_query():
    return {
        "answerable": True,
        "reasoning": "Group shops by the verified related category name.",
        "table": "categories",
        "joins": [{
            "table": "shop_details",
            "join_type": "LEFT",
            "relationship": {
                "constraint_name": "shops_category_fk",
                "table": "shop_details",
                "referenced_table": "categories",
            },
        }],
        "select_items": [
            {"table": "categories", "column": "categoriesname", "aggregate": None, "alias": "category_name"},
            {"table": "shop_details", "column": "id", "aggregate": "COUNT", "distinct": True, "alias": "shop_count"},
        ],
        "where_conditions": [],
        "combine_with": "AND",
        "group_by": [        {"table": "categories", "column": "categoriesname"}],
        "count_entity": {"table": "shop_details", "column": "id"},
        "order_by": [{"column": "shop_count", "direction": "DESC"}],
    }


def _complete_category_booking_sum_query(**overrides):
    query = {
        "answerable": True,
        "reasoning": "Show every category with total booking amount.",
        "table": "bookings",
        "joins": [
            {
                "table": "shop_details",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "reviewed_bookings_shop_id",
                    "table": "bookings",
                    "referenced_table": "shop_details",
                },
            },
            {
                "table": "categories",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "shops_category_fk",
                    "table": "shop_details",
                    "referenced_table": "categories",
                },
            },
        ],
        "select_items": [
            {
                "table": "categories",
                "column": "id",
                "aggregate": None,
                "alias": "category_id",
            },
            {
                "table": "categories",
                "column": "categoriesname",
                "aggregate": None,
                "alias": "category_name",
            },
            {
                "table": "bookings",
                "column": "final_amount",
                "aggregate": "SUM",
                "alias": "total_booking_amount",
            },
        ],
        "where_expression": None,
        "group_by": [
            {"table": "categories", "column": "id"},
            {"table": "categories", "column": "categoriesname"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [
            {"column": "total_booking_amount", "direction": "DESC"}
        ],
        "include_empty_groups": True,
    }
    query.update(overrides)
    return query


def test_query_generation_accepts_verified_join_and_normalizes_it():
    normalized = _validate_llm_output(_category_count_query(), SCHEMA, RELATIONSHIPS)

    assert normalized["joins"][0]["table"] == "shop_details"
    assert normalized["select_items"][0]["table"] == "categories"
    assert normalized["select_items"][1]["distinct"] is True
    assert normalized["count_entity"] == {"table": "shop_details", "column": "id"}
    assert normalized["order_by"] == [{"column": "shop_count", "direction": "DESC"}]


@pytest.mark.parametrize(
    ("aggregate_table", "join_table", "relationship_name", "relationship_table",
     "referenced_table", "expected_predicates"),
    [
        (
            "categories",
            "categories",
            "shops_category_fk",
            "shop_details",
            "categories",
            "aggregate_on_base_table=False all_joins_many_to_one=True",
        ),
        (
            "shop_details",
            "reviews",
            "reviewed_reviews_shop_id",
            "reviews",
            "shop_details",
            "aggregate_on_base_table=True all_joins_many_to_one=False",
        ),
    ],
)
def test_joined_numeric_aggregate_rejection_logs_failed_rule_only(
    caplog,
    aggregate_table,
    join_table,
    relationship_name,
    relationship_table,
    referenced_table,
    expected_predicates,
):
    query = {
        "answerable": True,
        "reasoning": "Aggregate evidence test.",
        "table": "shop_details",
        "joins": [{
            "table": join_table,
            "join_type": "LEFT",
            "relationship": {
                "constraint_name": relationship_name,
                "table": relationship_table,
                "referenced_table": referenced_table,
            },
        }],
        "select_items": [{
            "table": aggregate_table,
            "column": "id",
            "aggregate": "SUM",
            "alias": "total_id",
        }],
        "where_conditions": [{
            "table": "shop_details",
            "column": "name",
            "operator": "=",
            "value": "private-filter-value",
        }],
    }

    with caplog.at_level("WARNING", logger="app.agent.nodes.generate_query"):
        with pytest.raises(
            QueryGenerationError,
            match="use isolated_child_aggregates instead of a standard join",
        ):
            _validate_llm_output(
                query, SCHEMA, RELATIONSHIPS, attempt_number=2
            )

    rejection_log = next(
        record.message
        for record in caplog.records
        if "rejected joined numeric aggregate plan" in record.message
    )
    assert expected_predicates in rejection_log
    assert "attempt=2" in rejection_log
    assert "joined_table" in rejection_log
    assert "relationship_table" in rejection_log
    assert "referenced_table" in rejection_log
    assert "cardinality" in rejection_log
    assert "private-filter-value" not in rejection_log
    assert "Aggregate evidence test." not in rejection_log
    assert "where_conditions" not in rejection_log


def test_aggregate_diagnostic_logging_failure_does_not_change_rejection(monkeypatch):
    query = {
        "answerable": True,
        "reasoning": "Aggregate evidence test.",
        "table": "shop_details",
        "joins": [{
            "table": "categories",
            "join_type": "LEFT",
            "relationship": {
                "constraint_name": "shops_category_fk",
                "table": "shop_details",
                "referenced_table": "categories",
            },
        }],
        "select_items": [{
            "table": "categories",
            "column": "id",
            "aggregate": "SUM",
            "alias": "total_id",
        }],
        "where_conditions": [],
    }
    monkeypatch.setattr(
        "app.agent.nodes.generate_query.logger.warning",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("logger failure")),
    )

    with pytest.raises(
        QueryGenerationError,
        match="use isolated_child_aggregates instead of a standard join",
    ):
        _validate_llm_output(query, SCHEMA, RELATIONSHIPS, attempt_number=1)


def test_node6_logs_aggregate_predicates_for_each_validation_attempt(
    monkeypatch, caplog
):
    def invalid_plan():
        return {
            "answerable": True,
            "reasoning": "Aggregate evidence test.",
            "table": "shop_details",
            "joins": [{
                "table": "reviews",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "reviewed_reviews_shop_id",
                    "table": "reviews",
                    "referenced_table": "shop_details",
                },
            }],
            "select_items": [{
                "table": "reviews",
                "column": "rating",
                "aggregate": "SUM",
                "alias": "total_rating",
            }],
            "where_conditions": [],
        }

    class StubLLM:
        def __init__(self):
            self.calls = 0

        async def call_agent_json(self, *args, **kwargs):
            self.calls += 1
            return invalid_plan()

    llm = StubLLM()
    monkeypatch.setattr(
        "app.agent.nodes.generate_query.get_llm_service", lambda: llm
    )

    with caplog.at_level("WARNING", logger="app.agent.nodes.generate_query"):
        with pytest.raises(
            QueryGenerationError,
            match="failed validation after 3 attempt",
        ):
            asyncio.run(generate_query(
                "Show an aggregate over joined data.",
                SCHEMA,
                [],
                RELATIONSHIPS,
            ))

    diagnostics = [
        record.message
        for record in caplog.records
        if "rejected joined numeric aggregate plan" in record.message
    ]
    assert llm.calls == 3
    assert len(diagnostics) == 3
    assert "attempt=1" in diagnostics[0]
    assert "attempt=2" in diagnostics[1]
    assert "attempt=3" in diagnostics[2]
    assert all(
        "aggregate_on_base_table=False" in diagnostic
        and "all_joins_many_to_one=False" in diagnostic
        for diagnostic in diagnostics
    )


def test_each_category_grouping_includes_category_primary_key():
    normalized = _validate_llm_output(
        _category_count_query(),
        SCHEMA,
        RELATIONSHIPS,
        question="Count the number of shops per category_id in shop_details.",
    )

    assert normalized["group_by"] == [
        {"table": "categories", "column": "categoriesname"},
        {"table": "categories", "column": "id"},
    ]


def test_at_least_one_joined_entity_requires_a_match_for_left_join():
    query = {
        "answerable": True,
        "reasoning": "Count shops with at least one offer per category.",
        "table": "categories",
        "joins": [
            {
                "table": "shop_details",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "shops_category_fk",
                    "table": "shop_details",
                    "referenced_table": "categories",
                },
            },
            {
                "table": "offer",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "reviewed_offer_shop_id",
                    "table": "offer",
                    "referenced_table": "shop_details",
                },
            },
        ],
        "select_items": [
            {
                "table": "categories",
                "column": "categoriesname",
                "aggregate": None,
                "alias": "category_name",
            },
            {
                "table": "shop_details",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": True,
                "alias": "shop_count",
            },
        ],
        "where_expression": None,
        "group_by": [{"table": "categories", "column": "categoriesname"}],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": "shop_details", "column": "id"},
        "order_by": [],
    }
    question = (
        "Count the number of shops per category_id where the shop has at least one offer."
    )

    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS, question=question)
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert normalized["group_by"] == [
        {"table": "categories", "column": "categoriesname"},
        {"table": "categories", "column": "id"},
    ]
    assert normalized["where_expression"] == {
        "table": "offer",
        "column": "id",
        "operator": "IS NOT NULL",
    }
    assert "WHERE `offer`.`id` IS NOT NULL" in built["sql"]


def test_at_least_one_joined_entity_is_implied_by_inner_join():
    query = {
        "answerable": True,
        "reasoning": "List shops with at least one offer.",
        "table": "shop_details",
        "joins": [{
            "table": "offer",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_offer_shop_id",
                "table": "offer",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    normalized = _validate_llm_output(
        query,
        SCHEMA,
        RELATIONSHIPS,
        question="List shops with at least one offer.",
    )

    assert normalized["where_expression"] is None


def test_booking_value_semantics_come_from_business_rule_catalog():
    rule = next(
        rule
        for rule in find_relevant_rules("Which shop has the highest booking value?")
        if rule["term"] == "booking_amount"
    )
    assert "bookings.final_amount" in rule["definition"]


def test_joined_having_count_is_safe_only_for_verified_many_to_one_chain():
    query = {
        "answerable": True,
        "reasoning": "List shops that have more than two offers.",
        "table": "shop_details",
        "joins": [
            {
                "table": "categories",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "shops_category_fk",
                    "table": "shop_details",
                    "referenced_table": "categories",
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
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": {
            "table": "categories",
            "column": "categoriesname",
            "operator": "=",
            "value": "Transportation Services",
        },
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        "having_expression": {
            "table": "offer",
            "column": "id",
            "aggregate": "COUNT",
            "operator": ">",
            "value": 2,
        },
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "HAVING COUNT(`offer`.`id`) > %s" in built["sql"]
    assert built["params"][:2] == ["Transportation Services", 2]
    assert built["params"][-1] == built["row_limit"] + 1


def test_joined_having_count_rejects_an_additional_fanout_branch():
    query = {
        "answerable": True,
        "reasoning": "List shops with more than two offers.",
        "table": "shop_details",
        "joins": [
            {
                "table": "categories",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "shops_category_fk",
                    "table": "shop_details",
                    "referenced_table": "categories",
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
            {
                "table": "reviews",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "reviewed_reviews_shop_id",
                    "table": "reviews",
                    "referenced_table": "shop_details",
                },
            },
        ],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": None,
        "group_by": [{"table": "shop_details", "column": "id"}],
        "having_expression": {
            "table": "offer",
            "column": "id",
            "aggregate": "COUNT",
            "operator": ">",
            "value": 2,
        },
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)

    with pytest.raises(
        SafetyViolationError,
        match="single many-to-one chain",
    ):
        run_safety_check(
            normalized,
            valid_tables=list(SCHEMA),
            schema_context=SCHEMA,
            relationships=RELATIONSHIPS,
        )

def test_subscription_questions_use_normal_schema_grading(monkeypatch):
    import app.agent.nodes.grade_sample as grade_sample

    retrieval_sizes = []
    grader_calls = []

    async def fake_retrieval(question, top_k):
        retrieval_sizes.append(top_k)
        return ["users"]

    async def fake_grader(*args, **kwargs):
        grader_calls.append(args[0])
        return False, [{
            "type": "relationship",
            "detail": "users and subscriptions",
            "tables": ["users", "subscriptions"],
        }]

    monkeypatch.setattr(
        grade_sample,
        "get_cached_schema",
        lambda: {"users": [{"column": "id", "key": "PRI"}]},
    )
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", fake_retrieval)
    monkeypatch.setattr(
        grade_sample,
        "_ask_cheap_model_if_sufficient",
        fake_grader,
    )

    question = "How many users have an active subscription and a plan assigned?"
    with pytest.raises(grade_sample.GradeSampleError):
        asyncio.run(grade_sample.grade_schema_sufficiency(question))

    assert retrieval_sizes == [6, 12, 18]
    assert grader_calls == [question, question, question]


def test_query_generation_rejects_unverified_or_disconnected_joins():
    query = _category_count_query()
    query["joins"][0]["relationship"]["constraint_name"] = "guessed_fk"
    with pytest.raises(QueryGenerationError, match="verified schema catalog"):
        _validate_llm_output(query, SCHEMA, RELATIONSHIPS)

    query = _category_count_query()
    query["table"] = "unrelated"
    with pytest.raises(QueryGenerationError, match="connect it"):
        _validate_llm_output(query, SCHEMA, RELATIONSHIPS)


def test_confirmed_offer_shop_relationship_builds_verified_join():
    query = {
        "answerable": True,
        "reasoning": "Join each offer to its owning shop.",
        "table": "shop_details",
        "joins": [{
            "table": "offer",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": "reviewed_offer_shop_id",
                "table": "offer",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)
    result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "INNER JOIN `offer` ON `offer`.`shop_id` = `shop_details`.`id`" in result["sql"]


def test_single_predicate_group_is_unwrapped_and_count_star_is_safe():
    query = {
        "answerable": True,
        "reasoning": "Count users with the requested name.",
        "table": "users",
        "joins": [],
        "select_items": [{
            "table": "users",
            "column": "*",
            "aggregate": "COUNT",
            "alias": "user_count",
        }],
        "where_expression": {
            "boolean": "AND",
            "conditions": [{
                "table": "users",
                "column": "name",
                "operator": "=",
                "value": "Alice",
            }],
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)
    result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert normalized["where_expression"] == {
        "table": "users",
        "column": "name",
        "operator": "=",
        "value": "Alice",
    }
    assert "COUNT(*) AS `user_count`" in result["sql"]
    assert result["params"] == ["Alice", 201]


def test_numeric_filter_values_are_coerced_using_schema_types():
    schema = {
        "shop_details": [{"column": "category_id", "data_type": "int"}],
        "bookings": [{"column": "final_amount", "data_type": "decimal"}],
        "plans": [{"column": "name", "data_type": "varchar"}],
    }
    normalized = _normalize_filter_expression(
        {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "shop_details",
                    "column": "category_id",
                    "operator": "IN",
                    "value": ["2", "3"],
                },
                {
                    "table": "bookings",
                    "column": "final_amount",
                    "operator": "BETWEEN",
                    "value": ["-1.25", "0"],
                },
                {
                    "table": "plans",
                    "column": "name",
                    "operator": "=",
                    "value": "2",
                },
            ],
        },
        schema,
        {"shop_details", "bookings", "plans"},
    )

    assert normalized["conditions"][0]["value"] == [2, 3]
    assert all(type(value) is int for value in normalized["conditions"][0]["value"])
    assert normalized["conditions"][1]["value"] == [Decimal("-1.25"), Decimal("0")]
    assert all(
        isinstance(value, Decimal)
        for value in normalized["conditions"][1]["value"]
    )
    assert normalized["conditions"][2]["value"] == "2"


def test_plain_wildcard_row_selection_is_expanded_to_explicit_columns():
    normalized = _validate_llm_output(
        {
            "answerable": True,
            "reasoning": "List plan records.",
            "table": "plans",
            "joins": [],
            "select_items": [{
                "table": "plans",
                "column": "*",
                "aggregate": None,
                "alias": "plan_record",
            }],
            "where_expression": None,
            "group_by": [],
            "having_expression": None,
            "count_groups": False,
            "count_entity": None,
            "order_by": [],
        },
        SCHEMA,
        RELATIONSHIPS,
    )

    assert [item["column"] for item in normalized["select_items"]] == [
        "id",
        "name",
        "price",
        "status",
    ]
    assert all(item["aggregate"] is None for item in normalized["select_items"])
    assert all(item["alias"] != "plan_record" for item in normalized["select_items"])


def test_safety_check_rejects_plain_wildcard_row_selection():
    with pytest.raises(SafetyViolationError, match="Plain wildcard row selection"):
        run_safety_check(
            {
                "table": "plans",
                "select_items": [{
                    "table": "plans",
                    "column": "*",
                    "aggregate": None,
                    "alias": "plan_record",
                }],
                "where_expression": None,
                "group_by": [],
                "having_expression": None,
                "count_groups": False,
                "count_entity": None,
                "order_by": [],
            },
            valid_tables=list(SCHEMA),
            schema_context=SCHEMA,
            relationships=RELATIONSHIPS,
        )


def test_count_star_remains_allowed():
    result = run_safety_check(
        {
            "table": "plans",
            "select_items": [{
                "table": "plans",
                "column": "*",
                "aggregate": "COUNT",
                "distinct": False,
                "alias": "plan_count",
            }],
            "where_expression": None,
            "group_by": [],
            "having_expression": None,
            "count_groups": False,
            "count_entity": None,
            "order_by": [],
        },
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "COUNT(*) AS `plan_count`" in result["sql"]


def test_invalid_numeric_filter_value_is_rejected():
    with pytest.raises(QueryGenerationError, match="not valid for numeric column"):
        _normalize_filter_expression(
            {
                "table": "shop_details",
                "column": "category_id",
                "operator": "=",
                "value": "2.5",
            },
            {"shop_details": [{"column": "category_id", "data_type": "int"}]},
            {"shop_details"},
        )


def test_active_offers_use_the_configured_status_definition():
    question = "What is the total value of active offers, grouped by category?"
    preflight_refusals(question)
    rules = find_relevant_rules(question)

    assert any(rule["term"] == "active_offer_status" for rule in rules)


def test_subscription_plan_semantics_come_from_allowed_tables_in_catalog():
    rule = next(
        rule
        for rule in find_relevant_rules("Which plan is most popular among our users?")
        if rule["term"] == "subscription_plan"
    )
    assert "users.plan_id = plans.id" in rule["definition"]
    assert "users.status = 1" in rule["definition"]


def test_filter_group_rejects_empty_conditions():
    query = {
        "answerable": True,
        "reasoning": "Count users.",
        "table": "users",
        "joins": [],
        "select_items": [{
            "table": "users",
            "column": "*",
            "aggregate": "COUNT",
            "alias": "user_count",
        }],
        "where_expression": {"boolean": "AND", "conditions": []},
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    with pytest.raises(QueryGenerationError, match="at least one condition"):
        _validate_llm_output(query, SCHEMA, RELATIONSHIPS)


def test_current_offer_wording_is_not_refused_before_structured_resolution():
    preflight_refusals(
        "Count offers that are currently valid.",
        "How many offers are currently valid?",
    )


def test_date_only_bounds_expand_without_missing_helper_imports():
    assert _expand_date_only_bounds(
        "<=", "2026-02-28", "timestamp"
    ) == [("<", "2026-03-01")]


def test_numeric_rounding_and_user_limit_are_compiled_and_validated():
    schema = {
        **SCHEMA,
        "bookings": [
            *SCHEMA["bookings"],
            {"column": "final_amount", "data_type": "decimal"},
        ],
    }
    query = {
        "answerable": True,
        "reasoning": "Return the rounded average with the requested limit.",
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "bookings",
            "column": "final_amount",
            "aggregate": "AVG",
            "alias": "average_amount",
            "round_digits": 2,
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": 5,
    }

    normalized = _validate_llm_output(query, schema, RELATIONSHIPS)
    result = run_safety_check(
        normalized,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=RELATIONSHIPS,
    )

    assert "ROUND(AVG(`bookings`.`final_amount`), 2)" in result["sql"]
    assert result["params"] == [6]
    assert result["row_limit"] == 5


def test_single_table_grouped_count_normalizes_stray_count_entity_to_null():
    query = {
        "answerable": True,
        "reasoning": "Count shops grouped by the stored category id.",
        "table": "shop_details",
        "joins": [],
        "select_items": [
            {
                "table": "shop_details",
                "column": "category_id",
                "aggregate": None,
                "alias": "category_id",
            },
            {
                "table": "shop_details",
                "column": "id",
                "aggregate": "COUNT",
                "alias": "shop_count",
            },
        ],
        "where_conditions": [],
        "combine_with": "AND",
        "group_by": [{"table": "shop_details", "column": "category_id"}],
        "count_entity": {"table": "shop_details", "column": "id"},
        "order_by": [{"column": "shop_count", "direction": "DESC"}],
    }

    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)

    assert normalized["count_entity"] is None


def test_category_group_count_uses_verified_dimension_name_and_includes_empty_categories():
    single_table_query = {
        "answerable": True,
        "reasoning": "Count shops grouped by category id.",
        "table": "shop_details",
        "joins": [],
        "select_items": [
            {
                "table": "shop_details",
                "column": "category_id",
                "aggregate": None,
                "alias": "category_id",
            },
            {
                "table": "shop_details",
                "column": "id",
                "aggregate": "COUNT",
                "alias": "shop_count",
            },
        ],
        "where_conditions": [],
        "combine_with": "AND",
        "group_by": [{"table": "shop_details", "column": "category_id"}],
        "count_entity": None,
        "order_by": [{"column": "shop_count", "direction": "DESC"}],
    }

    normalized = _normalize_grouped_dimension_count(
        single_table_query,
        "Count shops for each category_id",
        SCHEMA,
        RELATIONSHIPS,
    )
    validated = _validate_llm_output(
        single_table_query,
        SCHEMA,
        RELATIONSHIPS,
        "Count shops for each category_id",
    )
    sql = run_safety_check(
        validated,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )["sql"]

    assert normalized["table"] == "categories"
    assert normalized["joins"][0]["table"] == "shop_details"
    assert normalized["joins"][0]["join_type"] == "LEFT"
    assert normalized["select_items"][0]["column"] == "categoriesname"
    assert normalized["select_items"][0]["alias"] == "category_name"
    assert normalized["select_items"][1]["distinct"] is True
    assert normalized["count_entity"] == {"table": "shop_details", "column": "id"}
    assert "FROM `categories` LEFT JOIN `shop_details`" in sql
    assert "`categories`.`categoriesname` AS `category_name`" in sql
    assert "COUNT(DISTINCT `shop_details`.`id`) AS `shop_count`" in sql


def test_query_generation_enforces_five_table_maximum():
    query = _category_count_query()
    query["joins"] = [
        {
            "table": table,
            "join_type": "INNER",
            "relationship": {
                "constraint_name": f"edge_{index}",
                "table": "shop_details",
                "referenced_table": table,
            },
        }
        for index, table in enumerate(
            ("categories", "subcategories", "regions", "countries", "unrelated")
        )
    ]

    with pytest.raises(QueryGenerationError, match="at most 5 tables"):
        _validate_llm_output(query, SCHEMA, RELATIONSHIPS)


def test_five_table_relationship_path_is_allowed():
    query = {
        "answerable": True,
        "reasoning": "Follow the declared key path.",
        "table": "shop_details",
        "joins": [
            {
                "table": "categories",
                "join_type": "INNER",
                "relationship": {
                    "constraint_name": "shops_category_fk",
                    "table": "shop_details",
                    "referenced_table": "categories",
                },
            },
            {
                "table": "subcategories",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "subcategories_category_fk",
                    "table": "subcategories",
                    "referenced_table": "categories",
                },
            },
            {
                "table": "regions",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "regions_subcategory_fk",
                    "table": "regions",
                    "referenced_table": "subcategories",
                },
            },
            {
                "table": "countries",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "countries_region_fk",
                    "table": "regions",
                    "referenced_table": "countries",
                },
            },
        ],
        "select_items": [
            {"table": "countries", "column": "name", "aggregate": None, "alias": "country_name"},
        ],
        "where_conditions": [],
        "combine_with": "AND",
        "group_by": [],
        "order_by": [],
    }

    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)
    result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert result["sql"].count(" JOIN ") == 4
    assert "FROM `shop_details`" in result["sql"]


def test_safety_check_builds_safe_join_sql_and_orders_by_aggregate_alias():
    normalized = _validate_llm_output(_category_count_query(), SCHEMA, RELATIONSHIPS)
    result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "FROM `categories` LEFT JOIN `shop_details` ON `shop_details`.`category_id` = `categories`.`id`" in result["sql"]
    assert "COUNT(DISTINCT `shop_details`.`id`) AS `shop_count`" in result["sql"]
    assert "GROUP BY `categories`.`categoriesname`" in result["sql"]
    assert (
        "ORDER BY `shop_count` DESC, `categories`.`categoriesname` ASC"
        in result["sql"]
    )
    assert result["params"] == [1001]
    assert result["row_limit"] == 1000


def test_example_queries_for_restaurants_websites_and_highest_plan_price():
    restaurant_query = _validate_llm_output(
        {
            "answerable": True,
            "reasoning": "Restaurants are identified by their known subcategory.",
            "table": "shop_details",
            "joins": [],
            "select_items": [
                {"table": "shop_details", "column": "name", "aggregate": None, "alias": "name"},
                {"table": "shop_details", "column": "phone", "aggregate": None, "alias": "phone"},
            ],
            "where_conditions": [{
                "table": "shop_details", "column": "subcategory_id", "operator": "=", "value": 9,
            }],
            "combine_with": "AND",
            "group_by": [],
            "count_entity": None,
            "order_by": [{"table": "shop_details", "column": "name", "direction": "ASC"}],
        },
        SCHEMA,
        RELATIONSHIPS,
    )
    restaurant_sql = run_safety_check(
        restaurant_query, list(SCHEMA), SCHEMA, RELATIONSHIPS
    )
    assert "`shop_details`.`name` AS `name`" in restaurant_sql["sql"]
    assert "`shop_details`.`phone` AS `phone`" in restaurant_sql["sql"]
    assert "WHERE `shop_details`.`subcategory_id` = %s" in restaurant_sql["sql"]
    assert restaurant_sql["params"] == [9, 201]

    website_query = _validate_llm_output(
        {
            "answerable": True,
            "reasoning": "Count shops that have a non-null website link.",
            "table": "shop_details",
            "joins": [],
            "select_items": [{
                "table": "shop_details",
                "column": "id",
                "aggregate": "COUNT",
                "alias": "shop_count",
            }],
            "where_conditions": [{
                "table": "shop_details",
                "column": "websitelink",
                "operator": "IS NOT NULL",
                "value": None,
            }],
            "combine_with": "AND",
            "group_by": [],
            "count_entity": None,
            "order_by": [],
        },
        SCHEMA,
        RELATIONSHIPS,
    )
    website_sql = run_safety_check(website_query, list(SCHEMA), SCHEMA, RELATIONSHIPS)
    assert "COUNT(`shop_details`.`id`) AS `shop_count`" in website_sql["sql"]
    assert "WHERE `shop_details`.`websitelink` IS NOT NULL" in website_sql["sql"]

    plan_query = _validate_llm_output(
        {
            "answerable": True,
            "reasoning": "Return the maximum price across plan rows.",
            "table": "plans",
            "joins": [],
            "select_items": [{
                "table": "plans",
                "column": "price",
                "aggregate": "MAX",
                "alias": "max_price",
            }],
            "where_conditions": [],
            "combine_with": "AND",
            "group_by": [],
            "count_entity": None,
            "order_by": [],
        },
        SCHEMA,
        RELATIONSHIPS,
    )
    plan_sql = run_safety_check(plan_query, list(SCHEMA), SCHEMA, RELATIONSHIPS)
    assert "MAX(`plans`.`price`) AS `max_price`" in plan_sql["sql"]


def test_joined_count_requires_the_declared_counted_entity():
    query = _category_count_query()
    query.pop("count_entity")
    with pytest.raises(QueryGenerationError, match="identify the entity"):
        _validate_llm_output(query, SCHEMA, RELATIONSHIPS)

    query = _category_count_query()
    query["count_entity"] = {"table": "categories", "column": "id"}
    with pytest.raises(QueryGenerationError, match="declared count_entity"):
        _validate_llm_output(query, SCHEMA, RELATIONSHIPS)


def test_ast_requires_joined_count_to_match_the_count_entity():
    normalized = _validate_llm_output(_category_count_query(), SCHEMA, RELATIONSHIPS)
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    with pytest.raises(SafetyViolationError, match="validated count_entity"):
        _ast_verify(
            built["sql"],
            SCHEMA,
            RELATIONSHIPS,
            {"table": "categories", "column": "id"},
        )


def _unique_city_count_query():
    schema = {
        **SCHEMA,
        "shop_address": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
            {"column": "city", "data_type": "varchar", "key": ""},
            {"column": "is_default", "data_type": "tinyint", "key": ""},
        ],
    }
    relationships = [
        *RELATIONSHIPS,
        {
            "constraint_name": "reviewed_shop_address_shop_id",
            "table": "shop_address",
            "referenced_table": "shop_details",
            "cardinality": "many_to_one",
            "verification": "reviewed_manual_query",
            "column_pairs": [{
                "column": "shop_id",
                "referenced_column": "id",
            }],
        },
    ]
    query = {
        "answerable": True,
        "reasoning": "Count each default city once for every category.",
        "table": "categories",
        "joins": [
            {
                "table": "shop_details",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "shops_category_fk",
                    "table": "shop_details",
                    "referenced_table": "categories",
                },
            },
            {
                "table": "shop_address",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "reviewed_shop_address_shop_id",
                    "table": "shop_address",
                    "referenced_table": "shop_details",
                },
            },
        ],
        "select_items": [
            {
                "table": "categories",
                "column": "id",
                "aggregate": None,
                "alias": "category_id",
            },
            {
                "table": "categories",
                "column": "categoriesname",
                "aggregate": None,
                "alias": "category_name",
            },
            {
                "table": "shop_address",
                "column": "city",
                "aggregate": "COUNT",
                "distinct": True,
                "alias": "unique_city_count",
            },
        ],
        "where_expression": {
            "table": "shop_address",
            "column": "is_default",
            "operator": "=",
            "value": 1,
        },
        "group_by": [
            {"table": "categories", "column": "id"},
            {"table": "categories", "column": "categoriesname"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    return schema, relationships, query


def test_generator_accepts_distinct_city_count_without_count_entity():
    schema, relationships, query = _unique_city_count_query()

    normalized = _validate_llm_output(
        query,
        schema,
        relationships,
        question="Categories and the count of unique cities they operate in.",
    )

    assert normalized["count_entity"] is None
    assert normalized["select_items"][2] == {
        "table": "shop_address",
        "column": "city",
        "aggregate": "COUNT",
        "distinct": True,
        "alias": "unique_city_count",
    }


def test_generator_rejects_how_many_cities_as_joined_non_key_count():
    schema, relationships, query = _unique_city_count_query()

    with pytest.raises(QueryGenerationError) as info:
        _validate_llm_output(
            query,
            schema,
            relationships,
            question="Categories and how many cities they operate in.",
        )

    assert info.value.rule_id == "joined_count_not_primary_key"


def test_safety_layer_accepts_distinct_city_count_without_count_entity():
    schema, relationships, query = _unique_city_count_query()
    result = run_safety_check(
        query,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert "COUNT(DISTINCT `shop_address`.`city`) AS `unique_city_count`" in result["sql"]
    assert result["params"] == [1, 1001]


def test_non_distinct_joined_count_is_rejected_by_generator_and_safety_layer():
    schema, relationships, query = _unique_city_count_query()
    query["select_items"][2] = {
        "table": "shop_address",
        "column": "id",
        "aggregate": "COUNT",
        "distinct": False,
        "alias": "address_count",
    }
    query["count_entity"] = {"table": "shop_address", "column": "id"}

    with pytest.raises(QueryGenerationError) as generator_error:
        _validate_llm_output(query, schema, relationships)

    assert generator_error.value.rule_id == "joined_count_not_distinct"

    with pytest.raises(SafetyViolationError, match="must use DISTINCT"):
        run_safety_check(
            query,
            valid_tables=list(schema),
            schema_context=schema,
            relationships=relationships,
        )


def test_grouped_aggregate_has_stable_order_even_without_requested_sort():
    query = _category_count_query()
    query["order_by"] = []
    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)
    result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "ORDER BY `categories`.`categoriesname` ASC" in result["sql"]


def test_all_categories_sum_is_compiled_as_left_joined_fact_aggregate():
    raw_query = _complete_category_booking_sum_query(
        where_expression={
            "boolean": "AND",
            "conditions": [
                {
                    "table": "bookings",
                    "column": "final_amount",
                    "operator": ">",
                    "value": 10,
                },
                {
                    "table": "categories",
                    "column": "categoriesname",
                    "operator": "=",
                    "value": "Food & Dining",
                },
            ],
        }
    )
    normalized = _validate_llm_output(
        raw_query,
        SCHEMA,
        RELATIONSHIPS,
        question="Show every category with its total booking amount.",
    )
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    sql = built["sql"]

    assert normalized["include_empty_groups"] is True
    assert "FROM `categories` LEFT JOIN (" in sql
    assert "SUM(`bookings`.`final_amount`) AS `total_booking_amount`" in sql
    assert "MAX(`bookings`.`id`) AS `_eg_fact_marker`" in sql
    assert (
        "CASE WHEN `_empty_group_aggregates`.`_eg_fact_marker` IS NULL "
        "THEN 0 ELSE `_empty_group_aggregates`.`total_booking_amount` END"
    ) in sql
    assert "WHERE `bookings`.`final_amount` > %s" in sql
    assert ") AS `_empty_group_aggregates` ON " in sql
    assert "WHERE `categories`.`categoriesname` = %s" in sql
    assert sql.count("LIMIT %s") == 1
    assert built["params"] == [10, "Food & Dining", 1001]
    assert built["row_limit"] == 1000


def test_empty_group_mode_defaults_off_and_ignores_non_boolean_model_values():
    raw = _complete_category_booking_sum_query()
    raw.pop("include_empty_groups")
    normalized = _validate_llm_output(raw, SCHEMA, RELATIONSHIPS)
    assert normalized["include_empty_groups"] is False

    raw["include_empty_groups"] = "yes"
    normalized = _validate_llm_output(raw, SCHEMA, RELATIONSHIPS)
    assert normalized["include_empty_groups"] is False


def test_empty_group_mode_requires_explicit_exhaustive_question_wording():
    with pytest.raises(
        QueryGenerationError,
        match="explicit request for each, every, or all group members",
    ):
        _validate_llm_output(
            _complete_category_booking_sum_query(),
            SCHEMA,
            RELATIONSHIPS,
            question="Show total booking amount by category.",
        )

    normalized = _validate_llm_output(
        _complete_category_booking_sum_query(),
        SCHEMA,
        RELATIONSHIPS,
        question="Show every category with its total booking amount.",
    )
    assert normalized["include_empty_groups"] is True


def test_empty_group_mode_rejects_or_across_dimension_and_fact_filters():
    query = _validate_llm_output(
        _complete_category_booking_sum_query(
            where_expression={
                "boolean": "OR",
                "conditions": [
                    {
                        "table": "bookings",
                        "column": "final_amount",
                        "operator": ">",
                        "value": 10,
                    },
                    {
                        "table": "categories",
                        "column": "categoriesname",
                        "operator": "=",
                        "value": "Food & Dining",
                    },
                ],
            }
        ),
        SCHEMA,
        RELATIONSHIPS,
    )
    with pytest.raises(SafetyViolationError, match="OR filter cannot mix"):
        run_safety_check(
            query,
            valid_tables=list(SCHEMA),
            schema_context=SCHEMA,
            relationships=RELATIONSHIPS,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"limit": 5},
        {"having_expression": {
            "table": "bookings",
            "column": "final_amount",
            "aggregate": "SUM",
            "operator": ">",
            "value": 10,
        }},
        {"joins": [
            {
                "table": "shop_details",
                "join_type": "LEFT",
                "relationship": {
                    "constraint_name": "reviewed_bookings_shop_id",
                    "table": "bookings",
                    "referenced_table": "shop_details",
                },
            },
            _complete_category_booking_sum_query()["joins"][1],
        ]},
    ],
)
def test_empty_group_mode_rejects_limited_having_or_left_join_shapes(change):
    query = _validate_llm_output(
        _complete_category_booking_sum_query(**change),
        SCHEMA,
        RELATIONSHIPS,
    )
    with pytest.raises(SafetyViolationError):
        run_safety_check(
            query,
            valid_tables=list(SCHEMA),
            schema_context=SCHEMA,
            relationships=RELATIONSHIPS,
        )


def test_empty_group_mode_rejects_count_plans():
    raw = _complete_category_booking_sum_query()
    raw["select_items"][2] = {
        "table": "bookings",
        "column": "id",
        "aggregate": "COUNT",
        "distinct": True,
        "alias": "booking_count",
    }
    raw["order_by"] = [{"column": "booking_count", "direction": "DESC"}]
    raw["count_entity"] = {"table": "bookings", "column": "id"}
    query = _validate_llm_output(raw, SCHEMA, RELATIONSHIPS)
    with pytest.raises(SafetyViolationError, match="entity-count plans"):
        run_safety_check(
            query,
            valid_tables=list(SCHEMA),
            schema_context=SCHEMA,
            relationships=RELATIONSHIPS,
        )


def test_empty_group_ast_rejects_wrong_dimension_join_key():
    query = _validate_llm_output(
        _complete_category_booking_sum_query(),
        SCHEMA,
        RELATIONSHIPS,
    )
    built = run_safety_check(
        query,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )
    tampered = built["sql"].replace(
        "`categories`.`id` = `_empty_group_aggregates`.`_eg_group_0`",
        "`categories`.`categoriesname` = "
        "`_empty_group_aggregates`.`_eg_group_0`",
    )
    with pytest.raises(SafetyViolationError, match="grouping entity primary key"):
        from app.agent.nodes.safety_check import _ast_verify

        _ast_verify(
            tampered,
            SCHEMA,
            RELATIONSHIPS,
            query.get("count_entity"),
            expected_query=query,
        )


def test_ast_rejects_join_not_matching_verified_relationship():
    sql = (
        "SELECT `categories`.`categoriesname` AS `category_name` "
        "FROM `shop_details` INNER JOIN `categories` "
        "ON `shop_details`.`id` = `categories`.`categoriesname` LIMIT 10"
    )
    with pytest.raises(SafetyViolationError, match="verified foreign-key relationship"):
        _ast_verify(sql, SCHEMA, RELATIONSHIPS)


def test_schema_relationship_expansion_includes_related_candidates():
    from app.agent.nodes.grade_sample import _expand_schema_for_relationships

    selected, relationships = _expand_schema_for_relationships(
        SCHEMA, ["shop_details"], RELATIONSHIPS, "shops by category"
    )

    assert "categories" in selected
    assert any(edge["table"] == "shop_details" for edge in relationships)
    assert "unrelated" not in selected


def test_schema_relationship_validation_preserves_composite_keys_and_rejects_unknown_columns(
    monkeypatch,
):
    import app.agent.nodes.schema_grounding as schema_grounding

    monkeypatch.setattr(schema_grounding, "CURATED_RELATIONSHIP_OVERRIDES", ())
    from app.agent.nodes.schema_grounding import (
        SchemaGroundingError,
        _validated_relationships,
    )

    tables = {
        "left_table": [{"column": "part_a"}, {"column": "part_b"}],
        "right_table": [{"column": "key_a"}, {"column": "key_b"}],
    }
    declared = [{
        "constraint_name": "composite_fk",
        "table": "left_table",
        "referenced_table": "right_table",
        "column_pairs": [
            {"column": "part_a", "referenced_column": "key_a"},
            {"column": "part_b", "referenced_column": "key_b"},
        ],
    }]

    validated = _validated_relationships(tables, declared)
    assert len(validated) == 1
    assert len(validated[0]["column_pairs"]) == 2

    declared[0]["column_pairs"][0]["referenced_column"] = "missing"
    with pytest.raises(SchemaGroundingError, match="unknown column"):
        _validated_relationships(tables, declared)


def test_schema_relationship_validation_accepts_reviewed_invoice_plan_type_mismatch(
    monkeypatch,
):
    import app.agent.nodes.schema_grounding as schema_grounding

    relationship = next(
        edge for edge in schema_grounding.CURATED_RELATIONSHIP_OVERRIDES
        if edge["constraint_name"] == "reviewed_invoices_plan_id"
    )
    monkeypatch.setattr(
        schema_grounding, "CURATED_RELATIONSHIP_OVERRIDES", (relationship,)
    )
    tables = {
        "invoices": [{"column": "plan_id", "data_type": "int"}],
        "plans": [{"column": "id", "data_type": "bigint unsigned"}],
    }

    validated = schema_grounding._validated_relationships(tables, [])

    assert validated == [{
        "constraint_name": "reviewed_invoices_plan_id",
        "table": "invoices",
        "referenced_table": "plans",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "plan_id", "referenced_column": "id"},
        ],
    }]


def test_schema_relationship_validation_includes_reviewed_user_plan_and_offer_views(
    monkeypatch,
):
    import app.agent.nodes.schema_grounding as schema_grounding

    relationship_names = {
        "reviewed_users_plan_id",
        "reviewed_views_offer_id",
        "reviewed_editable_offers_view_shop_id",
        "reviewed_coupon_purchase_payment_user_id",
    }
    relationships = tuple(
        edge for edge in schema_grounding.CURATED_RELATIONSHIP_OVERRIDES
        if edge["constraint_name"] in relationship_names
    )
    monkeypatch.setattr(
        schema_grounding, "CURATED_RELATIONSHIP_OVERRIDES", relationships
    )
    tables = {
        "users": [{"column": "id"}, {"column": "plan_id"}],
        "plans": [{"column": "id"}],
        "views": [{"column": "offer_id"}],
        "offer": [{"column": "id"}],
        "editable_offers_view": [{"column": "shop_id"}],
        "shop_details": [{"column": "id"}],
        "coupon_purchase_payment": [{"column": "user_id"}],
    }

    validated = schema_grounding._validated_relationships(tables, [])

    assert {
        (
            edge["constraint_name"],
            edge["table"],
            edge["referenced_table"],
            tuple(
                (pair["column"], pair["referenced_column"])
                for pair in edge["column_pairs"]
            ),
        )
        for edge in validated
    } == {
        (
            "reviewed_users_plan_id",
            "users",
            "plans",
            (("plan_id", "id"),),
        ),
        (
            "reviewed_views_offer_id",
            "views",
            "offer",
            (("offer_id", "id"),),
        ),
        (
            "reviewed_editable_offers_view_shop_id",
            "editable_offers_view",
            "shop_details",
            (("shop_id", "id"),),
        ),
        (
            "reviewed_coupon_purchase_payment_user_id",
            "coupon_purchase_payment",
            "users",
            (("user_id", "id"),),
        ),
    }


def test_result_answer_uses_execution_truncation_and_handles_null_scalar():
    answer = _build_result_answer(
        {
            "results": [{"max_price": None}],
            "columns": ["max_price"],
            "row_count": 1,
            "truncated": False,
            "row_limit": 200,
        },
        {"group_by": []},
    )

    assert answer["answer_type"] == "single_value"
    assert "single_value" in answer
    assert answer["single_value"] is None
    assert answer["truncated"] is False


def test_summary_uses_only_deterministic_result_facts():
    summary = asyncio.run(generate_summary(
        "What is the highest price?",
        {
            "answer_type": "single_value",
            "single_value": 999,
            "results": [{"max_price": 999}],
            "columns": ["max_price"],
            "column_labels": {"max_price": "Max price"},
            "truncated": False,
        },
    ))
    sensitive_summary = asyncio.run(generate_summary(
        "What is the phone number?",
        {
            "answer_type": "single_value",
            "single_value": "5550100",
            "results": [{"phone": "5550100"}],
            "columns": ["phone"],
            "column_labels": {"phone": "Phone"},
            "truncated": False,
        },
    ))
    table_summary = asyncio.run(generate_summary(
        "List shops",
        {"answer_type": "table", "results": [{"name": "Shop A"}], "truncated": False},
    ))

    assert summary == "Max price: 999."
    assert sensitive_summary is None
    assert table_summary == "Returned 1 row."


def test_cache_discards_entries_from_an_old_answer_contract(monkeypatch):
    import time
    import app.agent.nodes.cache_check as cache_check

    monkeypatch.setattr(cache_check, "_cache", [{
        "question": "How many shops?",
        "key": "how many shops",
        "embedding": [],
        "final_answer": {"results": [{"count": 96}]},
        "timestamp": time.time(),
        "answer_version": 1,
    }])

    assert asyncio.run(check_cache("How many shops?")) is None
    assert cache_check._cache == []


def test_api_response_preserves_answer_quality_fields():
    response = _build_response(
        {
            "final_answer": {
                "results": [{"shop_count": 15}],
                "columns": ["shop_count"],
                "count": 1,
                "row_count": 1,
                "answer_type": "single_value",
                "single_value": 15,
                "column_labels": {"shop_count": "Shop count"},
                "column_formats": {"shop_count": "identifier"},
                "summary": "There are 15 shops.",
                "suggestions": [],
                "truncated": False,
                "row_limit": 200,
            },
            "cache_hit": False,
            "escalated": False,
        },
        "request-id",
    ).model_dump()

    assert response["summary"] == "There are 15 shops."
    assert response["column_labels"] == {"shop_count": "Shop count"}
    assert response["answer_type"] == "single_value"
    assert response["row_count"] == 1
    assert response["truncated"] is False
    assert response["column_formats"] == {"shop_count": "identifier"}
    assert response["suggestions"] == []


def test_api_response_preserves_no_data_details():
    suggestions = [{
        "column": "offer_heading",
        "original": "Lucky Users Special Offer",
        "suggested": "Lucky Users Special Offer!",
    }]
    response = _build_response(
        {
            "final_answer": {
                "results": [],
                "columns": ["average_amount"],
                "count": 0,
                "row_count": 0,
                "answer_type": "empty",
                "no_data_reason": "all_null",
                "matching_row_count": 3,
                "message": (
                    "The average is unavailable because the matching records "
                    "have no amount values."
                ),
                "suggestions": suggestions,
            },
            "cache_hit": True,
            "escalated": False,
        },
        "request-id",
    ).model_dump()

    assert response["answer_type"] == "empty"
    assert response["no_data_reason"] == "all_null"
    assert response["matching_row_count"] == 3
    assert response["message"].startswith("The average is unavailable")
    assert response["suggestions"] == [
        {**suggestions[0], "suggestion_id": None}
    ]
    assert response["cache_hit"] is True


def test_cache_replays_no_data_message_and_reason(monkeypatch):
    import time
    import app.agent.nodes.cache_check as cache_check

    final_answer = {
        "results": [],
        "answer_type": "empty",
        "no_data_reason": "no_rows",
        "message": "No data for this filter.",
    }
    monkeypatch.setattr(cache_check, "_cache", [{
        "question": "Average cancelled subscription amount",
        "key": "average cancelled subscription amount",
        "embedding": [],
        "final_answer": final_answer,
        "timestamp": time.time(),
        "answer_version": cache_check.CACHE_ENTRY_VERSION,
        "context_version": "default",
    }])

    cached = asyncio.run(check_cache("Average cancelled subscription amount"))

    assert cached == final_answer
    assert cached["no_data_reason"] == "no_rows"
    assert cached["message"] == "No data for this filter."


def test_normal_and_error_responses_have_the_same_contract():
    result_response = _build_response(
        {"final_answer": {"results": [], "columns": [], "count": 0, "answer_type": "empty"}},
        "request-id",
    ).model_dump()
    error_response = _build_error_response("request-id", "Request failed.")

    assert set(result_response) == set(ChatResponse.model_fields)
    assert set(error_response) == set(ChatResponse.model_fields)


def test_run_query_trims_probe_row_and_reports_truncation(monkeypatch):
    async def fake_fetch_all_readonly(sql, params):
        assert params == ()
        return [(1,), (2,), (3,), (4,)]

    monkeypatch.setattr(
        "app.agent.nodes.run_self_fix.fetch_all_readonly",
        fake_fetch_all_readonly,
    )

    result = asyncio.run(run_query({
        "sql": "SELECT `id` FROM `items` LIMIT 4",
        "params": (),
        "columns": ["id"],
        "row_limit": 3,
    }))

    assert result["results"] == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert result["row_count"] == 3
    assert result["truncated"] is True
    assert result["row_limit"] == 3


def test_run_query_does_not_report_truncation_at_exact_limit(monkeypatch):
    async def fake_fetch_all_readonly(sql, params):
        assert params == ()
        return [(1,), (2,), (3,)]

    monkeypatch.setattr(
        "app.agent.nodes.run_self_fix.fetch_all_readonly",
        fake_fetch_all_readonly,
    )

    result = asyncio.run(run_query({
        "sql": "SELECT `id` FROM `items` LIMIT 4",
        "params": (),
        "columns": ["id"],
        "row_limit": 3,
    }))

    assert result["results"] == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert result["row_count"] == 3
    assert result["truncated"] is False


def test_run_query_does_not_mark_a_user_requested_top_n_as_truncated(monkeypatch):
    async def fake_fetch_all_readonly(sql, params):
        assert params == ()
        return [(1,), (2,), (3,), (4,), (5,), (6,)]

    monkeypatch.setattr(
        "app.agent.nodes.run_self_fix.fetch_all_readonly",
        fake_fetch_all_readonly,
    )

    result = asyncio.run(run_query({
        "sql": "SELECT `id` FROM `items` LIMIT 6",
        "params": (),
        "columns": ["id"],
        "row_limit": 5,
        "user_limit": True,
    }))

    assert result["results"] == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]
    assert result["row_count"] == 5
    assert result["truncated"] is False


@pytest.mark.parametrize(
    ("matching_count", "expected_reason"),
    [(0, "no_rows"), (3, "all_null")],
)
def test_run_query_classifies_all_null_aggregate_with_matching_row_count(
    monkeypatch, matching_count, expected_reason
):
    executed = []

    async def fake_fetch_all_readonly(sql, params):
        executed.append((sql, params))
        if sql == "SELECT AVG(`amount`) FROM `payments`":
            return [(None,)]
        assert sql == "SELECT COUNT(*) FROM `payments` WHERE `status` = %s"
        assert params == ("cancelled",)
        return [(matching_count,)]

    monkeypatch.setattr(
        "app.agent.nodes.run_self_fix.fetch_all_readonly",
        fake_fetch_all_readonly,
    )

    result = asyncio.run(run_query({
        "sql": "SELECT AVG(`amount`) FROM `payments`",
        "params": (),
        "columns": ["average_amount"],
        "row_limit": 200,
        "count_sql": "SELECT COUNT(*) FROM `payments` WHERE `status` = %s",
        "count_params": ("cancelled",),
    }))

    assert result["results"] == []
    assert result["no_data_reason"] == expected_reason
    assert result["matching_row_count"] == matching_count
    assert len(executed) == 2


def test_no_data_answer_is_empty_and_uses_aggregate_specific_message():
    answer = _build_result_answer(
        {
            "results": [],
            "columns": ["average_amount"],
            "row_count": 0,
            "no_data_reason": "no_rows",
            "matching_row_count": 0,
        },
        {
            "group_by": [],
            "select_items": [{"aggregate": "AVG"}],
        },
    )

    assert answer["answer_type"] == "empty"
    assert answer["results"] == []
    assert answer["no_data_reason"] == "no_rows"
    assert answer["message"] == "No data for this filter."


@pytest.mark.parametrize(
    ("case_number", "execution"),
    [
        (
            19,
            {
                "results": [{
                    "shop_name": "Boss App Studio Pvt Ltd",
                    "total_booking_value": 82191.0,
                }],
                "truncated": False,
            },
        ),
        (21, {"results": [], "truncated": False}),
        (23, {"results": [{"count": 0}], "truncated": False}),
        (25, {"results": [], "no_data_reason": "no_rows"}),
    ],
)
def test_execution_benchmark_checks_expected_final_results(case_number, execution):
    assert _check_execution_expectation(case_number, execution) == "passed"


def test_execution_benchmark_redacts_sensitive_preview_values():
    preview = _safe_execution_preview([{
        "shop_name": "Example Shop",
        "total_booking_value": 123.45,
        "category_name": "Travel",
        "shop_count": 4,
    }])

    assert preview == [{
        "shop_name": "<redacted>",
        "total_booking_value": "<redacted>",
        "category_name": "<redacted>",
        "shop_count": 4,
    }]


def test_safety_check_builds_same_filter_count_query_for_scalar_aggregate():
    query = _validate_llm_output(
        {
            "answerable": True,
            "reasoning": "Average booking amount for a specific shop.",
            "table": "bookings",
            "joins": [],
            "select_items": [{
                "table": "bookings",
                "column": "final_amount",
                "aggregate": "AVG",
                "alias": "average_amount",
            }],
            "where_conditions": [{
                "table": "bookings",
                "column": "shop_id",
                "operator": "=",
                "value": 42,
            }],
            "combine_with": "AND",
            "group_by": [],
            "count_entity": None,
            "order_by": [],
        },
        SCHEMA,
        RELATIONSHIPS,
    )

    safe = run_safety_check(
        query,
        valid_tables=["bookings"],
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert safe["count_sql"] == (
        "SELECT /*+ MAX_EXECUTION_TIME(10000) */ COUNT(*) FROM `bookings` "
        "WHERE `bookings`.`shop_id` = %s"
    )
    assert safe["count_params"] == [42]


def test_all_null_average_answer_has_specific_message():
    answer = _build_result_answer(
        {
            "results": [],
            "columns": ["average_amount"],
            "row_count": 0,
            "no_data_reason": "all_null",
            "matching_row_count": 3,
        },
        {
            "group_by": [],
            "select_items": [{"aggregate": "AVG"}],
        },
    )

    assert answer["answer_type"] == "empty"
    assert answer["no_data_reason"] == "all_null"
    assert answer["matching_row_count"] == 3
    assert answer["message"] == (
        "The average is unavailable because the matching records have no amount values."
    )


def test_result_grader_accepts_only_verified_empty_aggregate_outcomes(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service",
        lambda: (_ for _ in ()).throw(
            AssertionError("Verified empty aggregates should bypass LLM grading.")
        ),
    )

    result = asyncio.run(grade_result(
        "Average cancelled subscription amount",
        {
            "results": [],
            "count": 0,
            "no_data_reason": "no_rows",
        },
    ))

    assert result == {"is_valid": True, "flagged": False, "flag_reason": None}


def test_result_grader_includes_sql_in_prompt(monkeypatch):
    captured = {}

    class FakeLLM:
        async def call_agent_json(self, agent, messages, temperature):
            captured["agent"] = agent
            captured["prompt"] = messages[0]["content"]
            captured["temperature"] = temperature
            return {"is_valid": True, "flag_reason": None}

    monkeypatch.setattr(
        "app.services.llm_service.get_llm_service",
        lambda: FakeLLM(),
    )

    sql = "SELECT name FROM products WHERE category = 'X'"
    result = asyncio.run(
        grade_result(
            "Show products in category X",
            {
                "results": [{"name": "Product"}],
                "columns": ["name"],
                "count": 1,
            },
            sql=sql,
        )
    )

    assert result["is_valid"] is True
    assert captured["agent"] == "RESULT_GRADER"
    assert captured["temperature"] == 0.0
    assert f"SQL that produced this result:\n{sql}" in captured["prompt"]


def test_count_groups_applies_average_threshold_to_each_shop_before_counting():
    query = {
        "answerable": True,
        "reasoning": "Count shops whose average review rating is greater than 4.5.",
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
        "select_items": [{
            "table": "reviews",
            "column": "*",
            "aggregate": "COUNT",
            "distinct": False,
            "alias": "qualifying_shop_count",
        }],
        "where_expression": None,
        "group_by": [{"table": "shop_details", "column": "id"}],
        "having_expression": {
            "aggregate": "AVG",
            "table": "reviews",
            "column": "rating",
            "operator": ">",
            "value": 4.5,
        },
        "count_groups": True,
        "count_entity": None,
        "order_by": [],
    }
    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)
    built = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert "COUNT(*) AS `qualifying_shop_count` FROM (SELECT" in built["sql"]
    assert "GROUP BY `shop_details`.`id` HAVING AVG(`reviews`.`rating`) > %s" in built["sql"]
    assert built["params"] == [4.5, 2]
    assert built["row_limit"] == 1

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE shop_details (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE reviews (id INTEGER PRIMARY KEY, shop_id INTEGER, rating REAL)"
        )
        for shop_id in range(1, 28):
            connection.execute("INSERT INTO shop_details (id) VALUES (?)", (shop_id,))
            connection.execute(
                "INSERT INTO reviews (shop_id, rating) VALUES (?, ?)",
                (shop_id, 5.0),
            )
            if shop_id > 21:
                connection.execute(
                    "INSERT INTO reviews (shop_id, rating) VALUES (?, ?)",
                    (shop_id, 4.0),
                )
        scalar = connection.execute(
            built["sql"].replace("%s", "?"), built["params"]
        ).fetchone()[0]
        incorrect_row_filter_count = connection.execute(
            "SELECT COUNT(DISTINCT shop_id) FROM reviews WHERE rating > ?",
            (4.5,),
        ).fetchone()[0]
    finally:
        connection.close()

    assert scalar == 21
    assert incorrect_row_filter_count == 27


def test_monthly_query_for_explicit_year_gets_complete_date_scope():
    schema = {
        **SCHEMA,
        "bookings": [
            *SCHEMA["bookings"],
            {"column": "time_slot_date", "data_type": "date"},
            {"column": "time_slot_month", "data_type": "int"},
            {"column": "created_at", "data_type": "timestamp"},
        ],
    }
    plan = {
        "answerable": True,
        "reasoning": "Group booking totals by month.",
        "table": "bookings",
        "joins": [],
        "select_items": [
            {
                "table": "bookings",
                "column": "time_slot_month",
                "aggregate": None,
                "alias": "month",
            },
            {
                "table": "bookings",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": False,
                "alias": "booking_count",
            },
            {
                "table": "bookings",
                "column": "final_amount",
                "aggregate": "SUM",
                "distinct": False,
                "alias": "total_revenue",
            },
        ],
        "where_expression": None,
        "group_by": [{"table": "bookings", "column": "time_slot_month"}],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    normalized = _validate_llm_output(
        plan,
        schema,
        [],
        question="Show bookings and revenue for each month of 2025.",
    )
    where = normalized["where_expression"]
    assert where == {
        "boolean": "AND",
        "conditions": [
            {
                "table": "bookings",
                "column": "time_slot_date",
                "operator": ">=",
                "value": "2025-01-01",
            },
            {
                "table": "bookings",
                "column": "time_slot_date",
                "operator": "<",
                "value": "2026-01-01",
            },
        ],
    }
    built = run_safety_check(
        normalized, list(schema), schema, relationships=[]
    )
    assert "`bookings`.`time_slot_date` >= %s" in built["sql"]
    assert "`bookings`.`time_slot_date` < %s" in built["sql"]
    assert built["params"][:2] == ["2025-01-01", "2026-01-01"]
    assert "MIN(`bookings`.`time_slot_date`) ASC" in built["sql"]
    assert "`bookings`.`time_slot_date` ASC" not in built["sql"]

    incorrect_plan = {
        **plan,
        "select_items": [
            {
                **plan["select_items"][0],
                "column": "created_at",
                "date_bucket": "month",
            },
            *plan["select_items"][1:],
        ],
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "bookings",
                    "column": "created_at",
                    "operator": ">=",
                    "value": "2025-01-01",
                },
                {
                    "table": "bookings",
                    "column": "created_at",
                    "operator": "<",
                    "value": "2026-01-01",
                },
            ],
        },
        "group_by": [{
            "table": "bookings",
            "column": "created_at",
            "date_bucket": "month",
        }],
        "order_by": [{"column": "month", "direction": "ASC"}],
    }
    normalized_incorrect_plan = _validate_llm_output(
        incorrect_plan,
        schema,
        [],
        question="Show the number of bookings and total revenue for each month of 2025.",
    )
    assert normalized_incorrect_plan["select_items"][0] == {
        "table": "bookings",
        "column": "time_slot_month",
        "aggregate": None,
        "distinct": False,
        "alias": "month",
    }
    assert normalized_incorrect_plan["group_by"] == [{
        "table": "bookings",
        "column": "time_slot_month",
    }]
    corrected = run_safety_check(
        normalized_incorrect_plan, list(schema), schema, relationships=[]
    )
    assert "`bookings`.`time_slot_date` >= %s" in corrected["sql"]
    assert "`bookings`.`time_slot_date` < %s" in corrected["sql"]
    assert "`bookings`.`created_at`" not in corrected["sql"]
    assert "GROUP BY `bookings`.`time_slot_month`" in corrected["sql"]
    assert "ORDER BY MIN(`bookings`.`time_slot_date`) ASC" in corrected["sql"]

    created_month_plan = _validate_llm_output(
        incorrect_plan,
        schema,
        [],
        question="Show the number of bookings created in each month of 2025.",
    )
    assert created_month_plan["select_items"][0]["column"] == "created_at"
    assert created_month_plan["group_by"] == [{
        "table": "bookings",
        "column": "created_at",
        "date_bucket": "month",
    }]


def test_timestamp_month_bucket_is_compiled_and_grouped_safely():
    schema = {
        **SCHEMA,
        "views": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "created_at", "data_type": "datetime"},
        ],
    }
    plan = {
        "answerable": True,
        "reasoning": "Count views by calendar month.",
        "table": "views",
        "joins": [],
        "select_items": [
            {
                "table": "views",
                "column": "created_at",
                "date_bucket": "month",
                "aggregate": None,
                "alias": "month",
            },
            {
                "table": "views",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": False,
                "alias": "view_count",
            },
        ],
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "table": "views",
                    "column": "created_at",
                    "operator": ">=",
                    "value": "2026-01-01",
                },
                {
                    "table": "views",
                    "column": "created_at",
                    "operator": "<",
                    "value": "2027-01-01",
                },
            ],
        },
        "group_by": [{
            "table": "views",
            "column": "created_at",
            "date_bucket": "month",
        }],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"column": "month", "direction": "ASC"}],
    }
    normalized = _validate_llm_output(plan, schema, [])
    built = run_safety_check(normalized, list(schema), schema, relationships=[])
    month_expression = (
        "CONCAT(YEAR(`views`.`created_at`), '-', "
        "LPAD(MONTH(`views`.`created_at`), 2, '0'))"
    )
    assert built["sql"].count(month_expression) == 3
    assert f"GROUP BY {month_expression}" in built["sql"]
    assert f"ORDER BY `month` ASC, {month_expression} ASC" in built["sql"]
    assert "ORDER BY `month` ASC, `views`.`created_at` ASC" not in built["sql"]
    assert "%Y" not in built["sql"]
    assert built["params"][:2] == ["2026-01-01", "2027-01-01"]

    raw_timestamp_order = {
        **normalized,
        "order_by": [{
            "table": "views",
            "column": "created_at",
            "direction": "ASC",
        }],
    }
    with pytest.raises(SafetyViolationError, match="GROUP BY column"):
        run_safety_check(
            raw_timestamp_order, list(schema), schema, relationships=[]
        )


def test_raw_timestamp_grouping_remains_rejected():
    schema = {
        **SCHEMA,
        "views": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "created_at", "data_type": "datetime"},
        ],
    }
    query = {
        "table": "views",
        "joins": [],
        "select_items": [
            {
                "table": "views",
                "column": "created_at",
                "aggregate": None,
                "alias": "created_at",
            },
            {
                "table": "views",
                "column": "id",
                "aggregate": "COUNT",
                "distinct": False,
                "alias": "view_count",
            },
        ],
        "where_expression": None,
        "group_by": [{"table": "views", "column": "created_at"}],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": None,
    }
    with pytest.raises(SafetyViolationError, match="Grouping by the raw time column"):
        run_safety_check(query, list(schema), schema, relationships=[])


def test_average_rating_contract_builds_the_required_count_query():
    question = "What is the number of shops with an average rating above 4.5?"
    rules = find_relevant_rules(question)

    assert {rule["term"] for rule in rules} == {
        "shops_above_average_rating_threshold",
        "list_shops_above_average_rating_threshold",
    }

    generated = asyncio.run(
        generate_query(question, SCHEMA, rules, RELATIONSHIPS)
    )
    built = run_safety_check(
        generated,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert generated["table"] == "reviews"
    assert generated["group_by"] == [{"table": "shop_details", "column": "id"}]
    assert generated["having_expression"]["aggregate"] == "AVG"
    assert generated["having_expression"]["operator"] == ">"
    assert generated["having_expression"]["value"] == 4.5
    assert generated["count_groups"] is True
    assert "HAVING AVG(`reviews`.`rating`) > %s" in built["sql"]
    assert built["params"] == [4.5, 2]


def test_average_rating_contract_lists_each_qualifying_shop():
    question = "Show shops with average rating above 4.5"
    rules = find_relevant_rules(question)

    assert [rule["term"] for rule in rules] == [
        "list_shops_above_average_rating_threshold"
    ]
    generated = asyncio.run(
        generate_query(question, SCHEMA, rules, RELATIONSHIPS)
    )
    built = run_safety_check(
        generated,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert generated["group_by"] == [
        {"table": "shop_details", "column": "id"},
        {"table": "shop_details", "column": "name"},
    ]
    assert generated["select_items"][2]["aggregate"] == "AVG"
    assert generated["select_items"][3]["distinct"] is True
    assert "COUNT(DISTINCT `reviews`.`id`)" in built["sql"]
    assert "HAVING AVG(`reviews`.`rating`) > %s" in built["sql"]
    assert built["params"] == [4.5, 1001]

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE shop_details (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute(
            "CREATE TABLE reviews (id INTEGER PRIMARY KEY, shop_id INTEGER, rating REAL)"
        )
        for shop_id in range(1, 28):
            connection.execute(
                "INSERT INTO shop_details (id, name) VALUES (?, ?)",
                (shop_id, f"shop-{shop_id}"),
            )
            connection.execute(
                "INSERT INTO reviews (shop_id, rating) VALUES (?, ?)",
                (shop_id, 5.0),
            )
            if shop_id > 21:
                connection.execute(
                    "INSERT INTO reviews (shop_id, rating) VALUES (?, ?)",
                    (shop_id, 4.0),
                )
        rows = connection.execute(
            built["sql"].replace("%s", "?"), built["params"]
        ).fetchall()
    finally:
        connection.close()

    assert len(rows) == 21
    assert all(row[2] > 4.5 for row in rows)


def test_average_rating_contract_clarifies_when_threshold_is_missing():
    question = "How many shops have an average rating above?"
    rules = find_relevant_rules(question)

    with pytest.raises(UnanswerableQuestionError, match="specify the numeric rating threshold"):
        asyncio.run(generate_query(question, SCHEMA, rules, RELATIONSHIPS))


@pytest.mark.parametrize(
    "question",
    [
        "How many bookings were created today?",
        "Show bookings from last week",
        "List reviews from the past 14 days",
    ],
)
def test_relative_date_wording_is_not_refused_before_resolution(question):
    preflight_refusals(question)


def test_cache_preserves_entries_and_isolates_context_versions(monkeypatch):
    from app.agent.nodes import cache_check
    import numpy as np

    async def fake_embed_single_text(_question):
        return np.array([1.0, 0.0])

    monkeypatch.setattr(cache_check, "embed_single_text", fake_embed_single_text)
    cache_check._cache.clear()
    answer = {"results": [{"count": 21}]}

    try:
        asyncio.run(cache_check.store("How many qualifying shops?", answer, "schema-v1"))

        assert len(cache_check._cache) == 1
        assert asyncio.run(
            cache_check.check_cache("How many qualifying shops?", "schema-v1")
        ) == answer
        assert asyncio.run(
            cache_check.check_cache("How many qualifying shops?", "schema-v2")
        ) is None
        assert asyncio.run(
            cache_check.check_cache("How many qualifying shops?", "schema-v1")
        ) == answer
    finally:
        cache_check._cache.clear()


def test_catalog_filters_are_part_of_cache_key():
    from app.agent.nodes import cache_check

    question = "Show juice shops in Nagercoil"
    drinks = [{
        "level": "subcategory",
        "value": "Drinks & Beverages",
        "exclude": False,
    }]
    snacks = [{
        "level": "subcategory",
        "value": "Snacks & Beverages",
        "exclude": False,
    }]

    assert cache_check._cache_key(question, drinks) != cache_check._cache_key(
        question, snacks
    )
    assert cache_check._cache_key(
        question,
        drinks + [{
            "level": "category",
            "value": "Food & Dining",
            "exclude": False,
        }],
    ) == cache_check._cache_key(
        question,
        [{
            "level": "category",
            "value": "Food & Dining",
            "exclude": False,
        }] + drinks,
    )
    assert cache_check._cache_key(question) == cache_check._normalize(question)
    assert cache_check._cache_key(
        question,
        [{"level": "subcategory", "value": "Drinks & Beverages", "exclude": False}],
    ) != cache_check._cache_key(
        question,
        [{"level": "subcategory", "value": "Drinks & Beverages", "exclude": True}],
    )
    assert cache_check._cache_key(
        question,
        drinks,
        "OR",
    ) != cache_check._cache_key(
        question,
        drinks,
        "AND",
    )
    assert cache_check._cache_key(
        question,
        drinks,
        "OR",
    ) == cache_check._cache_key(
        question,
        list(drinks),
        "or",
    )


def test_name_search_filters_have_distinct_cache_keys():
    from app.agent.nodes import cache_check

    question = "Show shops"
    juice = [{"kind": "name_search", "term": "juice"}]
    bakery = [{"kind": "name_search", "term": "bakery"}]
    assert cache_check._cache_key(question, juice) != cache_check._cache_key(
        question, bakery
    )
    assert cache_check._cache_key(question, juice) != cache_check._cache_key(
        question
    )


def test_cache_lookup_and_store_isolate_catalog_filter_choices(monkeypatch):
    from app.agent.nodes import cache_check
    import numpy as np

    async def fake_embed_single_text(_question):
        return np.array([1.0, 0.0])

    monkeypatch.setattr(cache_check, "embed_single_text", fake_embed_single_text)
    cache_check._cache.clear()
    question = "Show juice shops in Nagercoil"
    drinks = [{
        "level": "subcategory",
        "value": "Drinks & Beverages",
        "exclude": False,
    }]
    snacks = [{
        "level": "subcategory",
        "value": "Snacks & Beverages",
        "exclude": False,
    }]
    drinks_answer = {"results": [{"name": "Drink shop"}]}
    snacks_answer = {"results": [{"name": "Snack shop"}]}

    try:
        asyncio.run(cache_check.store(
            question, drinks_answer, catalog_filters=drinks
        ))
        assert asyncio.run(cache_check.check_cache(
            question, catalog_filters=drinks
        )) == drinks_answer
        assert asyncio.run(cache_check.check_cache(
            question, catalog_filters=snacks
        )) is None

        asyncio.run(cache_check.store(
            question, snacks_answer, catalog_filters=snacks
        ))
        assert asyncio.run(cache_check.check_cache(
            question, catalog_filters=snacks
        )) == snacks_answer
    finally:
        cache_check._cache.clear()


def test_cache_lookup_and_store_isolate_filter_combiners(monkeypatch):
    from app.agent.nodes import cache_check
    import numpy as np

    async def fake_embed_single_text(_question):
        return np.array([1.0, 0.0])

    monkeypatch.setattr(cache_check, "embed_single_text", fake_embed_single_text)
    cache_check._cache.clear()
    question = "Show shops classified under snacks and drinks"
    filters = [
        {"level": "subcategory", "value": "Snacks & Beverages", "exclude": False},
        {"level": "subcategory", "value": "Drinks & Beverages", "exclude": False},
    ]
    answer = {"results": [{"name": "Shop"}]}

    try:
        asyncio.run(cache_check.store(
            question,
            answer,
            catalog_filters=filters,
            catalog_filter_combiner="OR",
        ))
        assert asyncio.run(cache_check.check_cache(
            question,
            catalog_filters=filters,
            catalog_filter_combiner="OR",
        )) == answer
        assert asyncio.run(cache_check.check_cache(
            question,
            catalog_filters=filters,
            catalog_filter_combiner="AND",
        )) is None
    finally:
        cache_check._cache.clear()


def test_nested_boolean_filters_preserve_grouping_and_parameter_order():
    query = {
        "answerable": True,
        "reasoning": "Apply the grouped price alternatives and status filter.",
        "table": "plans",
        "joins": [],
        "select_items": [{
            "table": "plans",
            "column": "name",
            "aggregate": None,
            "alias": "plan_name",
        }],
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {
                    "boolean": "OR",
                    "conditions": [
                        {"table": "plans", "column": "price", "operator": ">", "value": 500},
                        {"table": "plans", "column": "price", "operator": "<", "value": 100},
                    ],
                },
                {"table": "plans", "column": "status", "operator": "=", "value": 1},
            ],
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    normalized = _validate_llm_output(query, SCHEMA, RELATIONSHIPS)
    result = run_safety_check(
        normalized,
        valid_tables=list(SCHEMA),
        schema_context=SCHEMA,
        relationships=RELATIONSHIPS,
    )

    assert (
        "WHERE ((`plans`.`price` > %s) OR (`plans`.`price` < %s)) "
        "AND (`plans`.`status` = %s)" in result["sql"]
    )
    assert result["params"] == [500, 100, 1, 201]


@pytest.mark.parametrize(
    "select_items",
    [
        [{"table": "users", "column": "password", "aggregate": None, "alias": "password"}],
        [{"table": "users", "column": "*", "aggregate": None, "alias": None}],
    ],
)
def test_query_safety_rejects_credentials_and_wildcard_exposure(select_items):
    with pytest.raises(SafetyViolationError, match="sensitive"):
        run_safety_check(
            {
                "table": "users",
                "joins": [],
                "select_items": select_items,
                "where_conditions": [],
                "group_by": [],
                "order_by": [],
            },
            valid_tables=list(SCHEMA),
            schema_context=SCHEMA,
        )

def test_shop_details_matches_the_word_shop():
    assert {"shop", "shops"} <= _table_name_forms("shop_details")


def test_at_least_one_shop_finds_the_shop_details_join():
    joins = [
        {"table": "shop_details", "join_type": "INNER"},
        {"table": "shop_address", "join_type": "INNER"},
        {"table": "reviews", "join_type": "INNER"},
    ]
    where = {"table": "reviews", "column": "rating", "operator": ">", "value": 4.5}
    result = _enforce_at_least_one_joined_entity(
        "Subcategories with at least one shop rated above 4.5", where, joins, {}
    )
    assert result == where


def test_at_least_one_base_table_entity_does_not_require_a_join():
    plan = {
        "answerable": True,
        "reasoning": "Show completed bookings.",
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "bookings",
            "column": "id",
            "aggregate": None,
            "alias": "booking_id",
        }],
        "where_conditions": [],
        "group_by": [],
        "order_by": [],
    }

    normalized = _validate_llm_output(
        plan,
        SCHEMA,
        RELATIONSHIPS,
        question="Show at least one completed booking.",
    )

    assert normalized["joins"] == []


@pytest.mark.parametrize(
    ("question", "joins"),
    [
        ("", []),
        (" \t ", []),
        (
            "Find cafés with at least one ACTIVE subscription!!!",
            [{"table": "subscriptions", "join_type": "INNER"}],
        ),
        ("Find a shop named 'At Least One Cafe'.", []),
        ('Find a shop named "At Least One Cafe".', []),
    ],
)
def test_at_least_one_handles_empty_unicode_and_quoted_business_names(
    question, joins
):
    where = {"table": "parent", "column": "status", "operator": "=", "value": 1}
    assert _enforce_at_least_one_joined_entity(question, where, joins, {}) == where


@pytest.mark.parametrize(
    ("question", "table"),
    [
        ("Find users with at least one active subscription.", "subscriptions"),
        ("List shops with at least one pending order.", "orders"),
        ("Find projects with at least one completed task.", "tasks"),
        ("Show products with at least one review.", "reviews"),
        ("List accounts with at least one open ticket.", "tickets"),
        ("Find users with at least one paid invoice.", "invoices"),
        ("Show teams with at least one member.", "members"),
        ("List projects with at least one attachment.", "attachments"),
        ("Find shops with at least one completed booking.", "bookings"),
        ("Show companies with at least one active employee.", "employees"),
        ("Find users with at least one subscription.", "subscriptions"),
        ("List accounts with at least one orders.", "orders"),
    ],
)
def test_at_least_one_matches_the_joined_entity_after_modifiers(question, table):
    joins = [{"table": table, "join_type": "INNER"}]
    where = {"table": "parent", "column": "status", "operator": "=", "value": 1}

    assert _enforce_at_least_one_joined_entity(
        question, where, joins, {}
    ) == where


@pytest.mark.parametrize(
    ("question", "joins"),
    [
        (
            "Find users with at least one active subscription.",
            [{"table": "invoices", "join_type": "INNER"}],
        ),
        (
            "Find shops with at least one completed booking.",
            [{"table": "reviews", "join_type": "INNER"}],
        ),
        (
            "Find accounts with at least one order.",
            [
                {"table": "orders", "join_type": "INNER"},
                {"table": "order", "join_type": "INNER"},
            ],
        ),
        (
            "Find users with at least one completed subscription.",
            [
                {"table": "subscriptions", "join_type": "INNER"},
                {"table": "subscription", "join_type": "LEFT"},
            ],
        ),
    ],
)
def test_at_least_one_rejects_unmatched_or_ambiguous_joined_entities(
    question, joins
):
    with pytest.raises(QueryGenerationError, match="exactly one joined entity"):
        _enforce_at_least_one_joined_entity(question, None, joins, {})