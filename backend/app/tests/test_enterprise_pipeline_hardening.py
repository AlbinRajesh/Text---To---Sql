from datetime import datetime
import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.agent.nodes import grade_sample
from app.agent.nodes.business_rules import find_relevant_rules
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    _apply_entity_resolution_constraints,
    _apply_resolved_temporal_constraints,
    _lint_structural_query_plan,
    _sanitize_plan_values,
    _validate_llm_output,
    generate_query,
    preflight_refusals,
)
from app.agent.relationship_overrides import CURATED_RELATIONSHIP_OVERRIDES
from app.services.date_resolver import (
    extract_relative_period_intents,
    resolve_temporal_intents,
)


def _edge(name, left, right, left_column, right_column):
    return {
        "constraint_name": name,
        "table": left,
        "referenced_table": right,
        "column_pairs": [{
            "column": left_column,
            "referenced_column": right_column,
        }],
    }


def test_users_plan_relationship_is_curated_and_verified():
    relationship = next(
        edge for edge in CURATED_RELATIONSHIP_OVERRIDES
        if edge["constraint_name"] == "reviewed_users_plan_id"
    )

    assert relationship == {
        "constraint_name": "reviewed_users_plan_id",
        "table": "users",
        "referenced_table": "plans",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "plan_id", "referenced_column": "id"}],
    }


def test_views_shop_relationship_is_schema_validated(monkeypatch):
    import app.agent.nodes.schema_grounding as schema_grounding
    from app.agent.nodes.safety_check import run_safety_check

    relationships_to_validate = tuple(
        edge for edge in CURATED_RELATIONSHIP_OVERRIDES
        if edge["constraint_name"] in {
            "reviewed_views_shop_id",
            "reviewed_shop_details_category_id",
        }
    )
    monkeypatch.setattr(
        schema_grounding,
        "CURATED_RELATIONSHIP_OVERRIDES",
        relationships_to_validate,
    )
    schema = {
        "categories": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "categoriesname", "data_type": "varchar"},
        ],
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "category_id", "data_type": "bigint"},
        ],
        "views": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint"},
        ],
    }
    relationships = schema_grounding._validated_relationships(schema, [])

    assert {
        "constraint_name": "reviewed_views_shop_id",
        "table": "views",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    } in relationships
    plan = _validate_llm_output(
        {
            "answerable": True,
            "reasoning": "Count views through each shop's category.",
            "table": "categories",
            "joins": [
                {
                    "table": "shop_details",
                    "join_type": "INNER",
                    "relationship": {
                        "constraint_name": "reviewed_shop_details_category_id",
                        "table": "shop_details",
                        "referenced_table": "categories",
                    },
                },
                {
                    "table": "views",
                    "join_type": "INNER",
                    "relationship": {
                        "constraint_name": "reviewed_views_shop_id",
                        "table": "views",
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
                    "table": "views",
                    "column": "id",
                    "aggregate": "COUNT",
                    "distinct": True,
                    "alias": "view_count",
                },
            ],
            "where_expression": None,
            "group_by": [
                {"table": "categories", "column": "id"},
                {"table": "categories", "column": "categoriesname"},
            ],
            "having_expression": None,
            "count_groups": False,
            "count_entity": {"table": "views", "column": "id"},
            "order_by": [{"column": "view_count", "direction": "DESC"}],
        },
        schema,
        relationships,
        question="Which shop categories get viewed the most?",
    )
    built = run_safety_check(
        plan,
        valid_tables=list(schema),
        schema_context=schema,
        relationships=relationships,
    )

    assert "`views`.`shop_id` = `shop_details`.`id`" in built["sql"]
    assert "`shop_details`.`category_id` = `categories`.`id`" in built["sql"]
    assert "COUNT(DISTINCT `views`.`id`)" in built["sql"]


def test_schema_expansion_includes_shortest_paths_between_retrieval_anchors():
    schema = {
        table: [{"column": "id"}]
        for table in ("users", "invoices", "reviews", "unrelated")
    }
    relationships = [
        _edge("user_invoice", "users", "invoices", "id", "user_id"),
        _edge("invoice_review", "invoices", "reviews", "id", "invoice_id"),
    ]

    selected, returned_edges = grade_sample._expand_schema_for_relationships(
        schema, ["users", "reviews"], relationships, "users reviews"
    )

    assert list(selected)[:3] == ["users", "invoices", "reviews"]
    assert set(selected) == {"users", "invoices", "reviews"}
    assert len(returned_edges) == 2


def test_unreachable_retrieval_anchor_is_pruned_to_keep_context_connected():
    schema = {name: [{"column": "id"}] for name in ("a", "b", "isolated")}
    selected, edges = grade_sample._expand_schema_for_relationships(
        schema,
        ["a", "isolated", "b"],
        [_edge("a_b", "a", "b", "id", "id")],
        "a b",
    )

    assert set(selected) == {"a", "b"}
    assert len(edges) == 1


def test_grade_schema_passes_bridge_table_to_schema_grader(monkeypatch):
    schema = {
        table: [{"column": "id", "data_type": "bigint", "key": "PRI"}]
        for table in ("a", "bridge", "c")
    }
    relationships = [
        _edge("a_bridge", "a", "bridge", "bridge_id", "id"),
        _edge("bridge_c", "bridge", "c", "c_id", "id"),
    ]
    captured = {}

    async def retrieve(_question, top_k=6, **_kwargs):
        return ["a", "c"]

    async def grade(_original, _rewritten, schema_text, edges, **_kwargs):
        captured["schema"] = schema_text
        captured["edges"] = edges
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: relationships)
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)
    monkeypatch.setattr(grade_sample, "_matched_business_rules", lambda _question: [])

    result = asyncio.run(grade_sample.grade_schema_sufficiency("a c"))

    assert "bridge: id" in captured["schema"]
    assert len(captured["edges"]) == 2
    assert set(result["schema"]) == {"a", "bridge", "c"}


def test_required_ontology_relationship_tables_are_schema_anchors(monkeypatch):
    schema = {
        "users": [{"column": "plan_id"}],
        "plans": [{"column": "id"}, {"column": "name"}],
    }
    relationships = [
        _edge("reviewed_users_plan_id", "users", "plans", "plan_id", "id")
    ]
    captured = {}

    async def retrieve(_question, top_k=6, **_kwargs):
        return ["users"]

    async def grade(_original, _rewritten, schema_text, edges, **_kwargs):
        captured["schema"] = schema_text
        captured["edges"] = edges
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: relationships)
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)
    monkeypatch.setattr(
        grade_sample,
        "_matched_business_rules",
        lambda _question: [{
            "entity_resolution": {
                "canonical_table": "users",
                "filters": [{"table": "plans", "column": "name"}],
                "related_entities": [{
                    "table": "plans",
                    "required": True,
                }],
            }
        }],
    )

    result = asyncio.run(
        grade_sample.grade_schema_sufficiency("Show premium plans")
    )

    assert set(result["schema"]) == {"users", "plans"}
    assert "plans: id, name" in captured["schema"]
    assert len(captured["edges"]) == 1


def test_optional_ontology_relationship_tables_remain_available_for_anti_joins(
    monkeypatch,
):
    schema = {
        "users": [{"column": "user_type"}],
        "plans": [{"column": "id"}, {"column": "price"}],
        "shop_details": [{"column": "partner_id"}, {"column": "status"}],
    }
    relationships = [
        _edge("reviewed_users_plan_id", "users", "plans", "plan_id", "id"),
        _edge(
            "reviewed_shop_details_partner_id",
            "shop_details",
            "users",
            "partner_id",
            "id",
        ),
    ]
    captured = {}
    question = "Show me all the partners who are on a paid plan but have 0 active shops."
    paid_partner_rule = next(
        rule for rule in find_relevant_rules(question)
        if rule["term"] == "paid_partner_zero_shops_audit"
    )

    async def retrieve(_question, top_k=6, **_kwargs):
        return ["users"]

    async def grade(_original, _rewritten, schema_text, edges, **_kwargs):
        captured["schema"] = schema_text
        captured["edges"] = edges
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: relationships)
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)
    monkeypatch.setattr(
        grade_sample,
        "_matched_business_rules",
        lambda _question: [paid_partner_rule],
    )

    result = asyncio.run(grade_sample.grade_schema_sufficiency(question))

    assert set(result["schema"]) == {"users", "plans", "shop_details"}
    assert len(captured["edges"]) == 2


def test_location_consumable_search_keeps_shop_address_and_category_on_first_attempt(
    monkeypatch,
):
    schema = {
        "shop_details": [{"column": "id", "data_type": "bigint", "key": "PRI"}],
        "shop_address": [{"column": "shop_id", "data_type": "bigint"}],
        "subcategories": [{"column": "id", "data_type": "bigint", "key": "PRI"}],
    }
    relationships = [
        _edge("address_shop", "shop_address", "shop_details", "shop_id", "id"),
        _edge("shop_subcategory", "shop_details", "subcategories", "id", "id"),
    ]
    captured = {}

    async def retrieve(_question, top_k=6, **_kwargs):
        return []

    async def grade(_original, _rewritten, schema_text, edges, **_kwargs):
        captured["schema"] = schema_text
        captured["edges"] = edges
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: relationships)
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)
    monkeypatch.setattr(grade_sample, "_matched_business_rules", lambda _question: [])

    result = asyncio.run(
        grade_sample.grade_schema_sufficiency(
            "Where can I get juice in Nagercoil?"
        )
    )

    assert set(result["schema"]) == set(schema)
    assert all(f"{table}:" in captured["schema"] for table in schema)
    assert len(captured["edges"]) == 2


def test_fk_column_equality_moves_from_where_into_verified_join():
    schema = {
        "bookings": [{"column": "shop_id"}],
        "shop_details": [{"column": "id"}],
    }
    relationships = [
        _edge("booking_shop", "bookings", "shop_details", "shop_id", "id")
    ]
    plan = {
        "table": "bookings",
        "joins": [],
        "where_expression": {
            "table": "bookings",
            "column": "shop_id",
            "operator": "=",
            "value": {
                "column_ref": {"table": "shop_details", "column": "id"}
            },
        },
        "select_items": [],
    }

    linted = _lint_structural_query_plan(plan, schema, relationships)

    assert linted["where_expression"] is None
    assert linted["joins"] == [{
        "table": "shop_details",
        "join_type": "INNER",
        "relationship": {
            "constraint_name": "booking_shop",
            "table": "bookings",
            "referenced_table": "shop_details",
        },
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
        }],
    }]


@pytest.mark.parametrize(
    "predicate",
    [
        {
            "value": {
                "column_reference": {"table": "shop_details", "column": "id"}
            }
        },
        {
            "column_reference": {"table": "shop_details", "column": "id"}
        },
        {"value": "shop_details.id"},
        {"value": {"table": "shop_details", "column": "id"}},
    ],
)
def test_column_reference_shapes_are_promoted_to_verified_joins(predicate):
    schema = {
        "bookings": [{"column": "shop_id"}],
        "shop_details": [{"column": "id"}],
    }
    relationships = [
        _edge("booking_shop", "bookings", "shop_details", "shop_id", "id")
    ]
    plan = {
        "table": "bookings",
        "joins": [],
        "where_expression": {
            "table": "bookings",
            "column": "shop_id",
            "operator": "=",
            **predicate,
        },
        "select_items": [],
    }

    linted = _lint_structural_query_plan(plan, schema, relationships)

    assert linted["where_expression"] is None
    assert [join["table"] for join in linted["joins"]] == ["shop_details"]
    assert linted["joins"][0]["conditions"] == [{
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


def test_plan_value_sanitizer_flattens_column_references_recursively():
    plan = {
        "where_expression": {
            "boolean": "AND",
            "conditions": [{
                "table": "bookings",
                "column": "shop_id",
                "operator": "=",
                "value": {
                    "column_reference": {
                        "table": "shop_details",
                        "column": "id",
                    },
                    "confidence": 0.99,
                },
            }],
        },
        "joins": [{
            "table": "shop_details",
            "conditions": [{
                "table": "shop_details",
                "column": "id",
                "operator": "=",
                "value": {"table": "bookings", "column": "shop_id"},
            }],
        }],
        "having_expression": {
            "table": "bookings",
            "column": "shop_id",
            "operator": "=",
            "value": {
                "column_ref": {
                    "table": "shop_details",
                    "column": "id",
                }
            },
        },
        "typed_date_value": {
            "scalar_subquery": "max_date",
            "table": "bookings",
            "column": "due_date",
        },
    }

    sanitized = _sanitize_plan_values(plan)

    assert sanitized["where_expression"]["conditions"][0]["value"] == (
        "shop_details.id"
    )
    assert sanitized["joins"][0]["conditions"][0]["value"] == {
        "column_reference": {
            "table": "bookings",
            "column": "shop_id",
        }
    }
    assert sanitized["having_expression"]["value"] == "shop_details.id"
    assert sanitized["typed_date_value"] == plan["typed_date_value"]
    assert isinstance(plan["where_expression"]["conditions"][0]["value"], dict)


def test_sanitized_llm_column_reference_promotes_before_plan_validation():
    schema = {
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
        ],
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
    }
    relationship = {
        **_edge("booking_shop", "bookings", "shop_details", "shop_id", "id"),
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
    }
    plan = _sanitize_plan_values({
        "answerable": True,
        "reasoning": "Use the verified shop relationship.",
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
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
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    })

    result = _validate_llm_output(plan, schema, [relationship])

    assert result["where_expression"] is None
    assert result["joins"][0]["relationship"]["constraint_name"] == (
        "booking_shop"
    )


def test_sanitized_column_reference_validates_on_first_generation_attempt(
    monkeypatch,
):
    schema = {
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
        ],
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar", "key": ""},
            {"column": "status", "data_type": "tinyint", "key": ""},
        ],
    }
    relationship = {
        **_edge("booking_shop", "bookings", "shop_details", "shop_id", "id"),
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
    }
    raw_plan = {
        "answerable": True,
        "reasoning": "Use the verified shop relationship.",
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
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
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }
    calls = []

    class FakeLlm:
        async def call_agent_json(self, *_args, **_kwargs):
            calls.append(1)
            return raw_plan

    monkeypatch.setattr(
        "app.agent.nodes.generate_query.get_llm_service",
        lambda: FakeLlm(),
    )

    result = asyncio.run(generate_query(
        "Show booking shop names.",
        schema,
        [],
        [relationship],
        original_question="Show booking shop names.",
    ))

    assert len(calls) == 1
    assert result["where_expression"] == {
        "table": "shop_details",
        "column": "status",
        "operator": "=",
        "value": 1,
    }
    assert result["joins"][0]["relationship"]["constraint_name"] == (
        "booking_shop"
    )


def test_linter_compiles_shortest_verified_multi_hop_join_skeleton():
    schema = {
        table: [{"column": column} for column in columns]
        for table, columns in {
            "offer": ["shop_id"],
            "shop_details": ["id", "subcategory_id"],
            "shop_address": ["shop_id", "city"],
            "subcategories": ["id", "subcategoryname"],
        }.items()
    }
    relationships = [
        _edge("offer_shop", "offer", "shop_details", "shop_id", "id"),
        _edge("shop_address", "shop_address", "shop_details", "shop_id", "id"),
        _edge(
            "shop_subcategory",
            "shop_details",
            "subcategories",
            "subcategory_id",
            "id",
        ),
    ]
    plan = {
        "table": "offer",
        "joins": [],
        "select_items": [{
            "table": "subcategories",
            "column": "subcategoryname",
        }],
        "where_expression": {
            "table": "shop_address",
            "column": "city",
            "operator": "=",
            "value": "Nagercoil",
        },
    }

    linted = _lint_structural_query_plan(plan, schema, relationships)

    assert [join["table"] for join in linted["joins"]] == [
        "shop_details",
        "shop_address",
        "subcategories",
    ]
    assert [
        join["relationship"]["constraint_name"] for join in linted["joins"]
    ] == ["offer_shop", "shop_address", "shop_subcategory"]


def test_plan_validation_runs_the_equi_join_linter_before_schema_checks():
    schema = {
        "bookings": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "shop_id", "data_type": "bigint", "key": ""},
        ],
        "shop_details": [
            {"column": "id", "data_type": "bigint", "key": "PRI"},
            {"column": "name", "data_type": "varchar", "key": ""},
        ],
    }
    relationship = {
        **_edge("booking_shop", "bookings", "shop_details", "shop_id", "id"),
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
    }
    plan = {
        "answerable": True,
        "reasoning": "Use the verified shop relationship.",
        "table": "bookings",
        "joins": [],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": {
            "table": "bookings",
            "column": "shop_id",
            "operator": "=",
            "value": {
                "column_ref": {"table": "shop_details", "column": "id"}
            },
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }

    result = _validate_llm_output(
        plan, schema, [relationship], matched_business_rules=[]
    )

    assert result["where_expression"] is None
    assert result["joins"][0]["table"] == "shop_details"


def test_unverified_column_equality_returns_prescriptive_error():
    plan = {
        "table": "bookings",
        "joins": [],
        "where_expression": {
            "table": "bookings",
            "column": "shop_id",
            "operator": "=",
            "value": {
                "column_ref": {"table": "users", "column": "id"}
            },
        },
        "select_items": [],
    }

    with pytest.raises(QueryGenerationError) as error:
        _lint_structural_query_plan(
            plan,
            {"bookings": [{"column": "shop_id"}], "users": [{"column": "id"}]},
            [],
        )
    assert str(error.value) == (
        "Structural Error: Column-to-column equality comparisons must be placed "
        "in the `joins` array as join conditions, NOT in `where_expression`. "
        "The `where_expression` only accepts literal constant values."
    )


def test_query_level_distinct_moves_to_a_single_count_select_item():
    plan = {
        "distinct": True,
        "select_items": [{
            "table": "bookings",
            "column": "id",
            "aggregate": "COUNT",
        }],
    }

    linted = _lint_structural_query_plan(plan, {}, [])

    assert "distinct" not in linted
    assert linted["select_items"][0]["distinct"] is True


def test_relative_period_is_resolved_and_applied_as_half_open_iso_range():
    intents = extract_relative_period_intents(
        "Show bookings created last month"
    )
    resolved = resolve_temporal_intents(
        intents,
        now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    query = _apply_resolved_temporal_constraints(
        {
            "table": "bookings",
            "joins": [],
            "where_expression": None,
        },
        resolved["date_ranges"],
        {"bookings": [{"column": "created_at", "data_type": "datetime"}]},
    )

    assert resolved["date_ranges"][0]["target"] == "bookings"
    assert query["where_expression"] == {
        "boolean": "AND",
        "conditions": [
            {
                "table": "bookings",
                "column": "created_at",
                "operator": ">=",
                "value": "2026-09-01",
            },
            {
                "table": "bookings",
                "column": "created_at",
                "operator": "<",
                "value": "2026-10-01",
            },
        ],
    }


def test_calendar_relative_phrases_are_no_longer_refused():
    for question in (
        "Show bookings from yesterday",
        "Show invoices from last month",
        "How many users were created this year",
    ):
        preflight_refusals(question)


def test_graph_classification_resolves_dates_before_query_generation(monkeypatch):
    from app.agent import graph

    async def classify(question, _context, **_options):
        return {
            "intent": "data",
            "original_question": question,
            "rewritten_question": question,
        }

    monkeypatch.setattr(graph, "classify_and_rewrite", classify)
    state = {"user_question": "Show bookings from last month"}

    asyncio.run(graph.node_classify(state))

    assert state["classifier_result"]["temporal_constraints"][0]["target"] == (
        "bookings"
    )
    assert state["classifier_result"]["temporal_constraints"][0]["start"]
    assert state["classifier_result"]["temporal_constraints"][0]["end"]


def test_graph_forwards_resolved_temporal_ranges_to_query_generator(monkeypatch):
    from app.agent import graph

    captured = {}
    constraints = [{
        "target": "bookings",
        "start": "2026-09-01",
        "end": "2026-10-01",
        "timezone": "Asia/Kolkata",
        "boundary": "[start,end)",
    }]

    async def generate(*_args, **kwargs):
        captured.update(kwargs)
        return {"table": "bookings"}

    monkeypatch.setattr(graph, "generate_query", generate)
    monkeypatch.setattr(graph, "resolve_value_candidates", lambda *_args, **_kwargs: [])
    state = {
        "classifier_result": {
            "rewritten_question": "Show bookings last month",
            "original_question": "Show bookings last month",
            "temporal_constraints": constraints,
        },
        "schema_context": {"bookings": []},
        "matched_business_rules": [],
        "schema_relationships": [],
    }

    asyncio.run(graph.node_generate_query(state))

    assert captured["temporal_constraints"] == constraints


def test_temporal_parser_normalizes_yesterday_this_year_and_previous_periods():
    intents = extract_relative_period_intents(
        "Show invoices from yesterday and users created this year"
    )

    assert [(item["target"], item["window"], item["unit"]) for item in intents] == [
        ("invoices", "yesterday", "day"),
        ("users", "current_year", "year"),
    ]


def test_ontology_entity_enforces_configured_canonical_filter():
    rule = next(
        item for item in find_relevant_rules("Show paid invoices")
        if item.get("entity_resolution", {}).get("entity") == "paid_invoice"
    )
    query = _apply_entity_resolution_constraints(
        {
            "table": "invoices",
            "joins": [],
            "where_expression": None,
        },
        [rule],
        {"invoices": [{"column": "status", "data_type": "varchar"}]},
    )

    assert query["where_expression"] == {
        "table": "invoices",
        "column": "status",
        "operator": "=",
        "value": "paid",
    }


def test_premium_plan_ontology_uses_users_grain_and_verified_plan_join():
    rule = next(
        item for item in find_relevant_rules("Show users with a premium plan")
        if item.get("entity_resolution", {}).get("entity") == "premium_plan"
    )
    relationship = {
        **_edge("reviewed_users_plan_id", "users", "plans", "plan_id", "id"),
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
    }
    query = _apply_entity_resolution_constraints(
        {
            "table": "users",
            "joins": [],
            "where_expression": None,
        },
        [rule],
        {
            "users": [
                {"column": "plan_id", "data_type": "bigint"},
                {"column": "status", "data_type": "tinyint"},
            ],
            "plans": [{"column": "name", "data_type": "varchar"}],
        },
        [relationship],
    )

    assert query["joins"][0]["relationship"]["constraint_name"] == (
        "reviewed_users_plan_id"
    )
    assert query["where_expression"] == {
        "boolean": "AND",
        "conditions": [
            {
                "table": "users",
                "column": "plan_id",
                "operator": "IS NOT NULL",
            },
            {
                "table": "users",
                "column": "status",
                "operator": "=",
                "value": 1,
            },
        ],
    }


def test_active_subscription_ontology_uses_users_assignment_filters():
    rule = next(
        item for item in find_relevant_rules("How many active subscriptions?")
        if item.get("entity_resolution", {}).get("entity") == "active_subscription"
    )
    assert rule["entity_resolution"]["canonical_table"] == "users"
    query = _apply_entity_resolution_constraints(
        {
            "table": "users",
            "joins": [],
            "where_expression": None,
        },
        [rule],
        {
            "users": [
                {"column": "plan_id", "data_type": "bigint"},
                {"column": "status", "data_type": "tinyint"},
            ],
        },
    )

    assert query["where_expression"] == {
        "boolean": "AND",
        "conditions": [
            {
                "table": "users",
                "column": "plan_id",
                "operator": "IS NOT NULL",
            },
            {
                "table": "users",
                "column": "status",
                "operator": "=",
                "value": 1,
            },
        ],
    }


def test_subscription_plan_canonical_business_rule_is_restored():
    rule = next(
        item for item in find_relevant_rules(
            "How many users have a subscription plan?"
        )
        if item.get("term") == "subscription_plan"
    )

    assert "users.plan_id IS NOT NULL" in rule["definition"]
    assert "without joining plans" in rule["definition"]
    assert rule["synonyms"] == [
        "users with a subscription plan",
        "users have a subscription plan",
        "subscribed users",
        "users with a plan",
    ]

    ontology = next(
        item for item in json.loads(
            Path(__file__).parents[1].joinpath(
                "rules", "business_rules.json"
            ).read_text(encoding="utf-8")
        )["ontology_entities"]
        if item.get("entity") == "subscription_plan"
    )
    assert ontology["canonical_table"] == "users"
    assert "subscription plan" not in ontology["synonyms"]
    assert ontology["related_entities"] == []


def test_plan_subscription_existence_rule_forbids_ungrounded_tier_literals():
    rule = next(
        item for item in find_relevant_rules("Who bought a premium plan?")
        if item.get("term") == "plan_subscription_existence"
    )

    assert "users.plan_id IS NOT NULL" in rule["definition"]
    assert "grounded value catalog" in rule["definition"]


def test_restaurant_subcategory_rule_grounds_the_seed_spelling():
    rule = next(
        item for item in find_relevant_rules("dining")
        if item.get("term") == "subcategory_restaurant"
    )

    assert rule["synonyms"] == ["restaurant", "restaurants", "dining"]
    assert rule["definition"] == (
        "Maps restaurant search terms to the official database "
        "subcategory value 'Restaurent'."
    )


@pytest.mark.parametrize(
    ("question", "term", "canonical_table", "related_table", "relationship"),
    [
        (
            "active shops in Kochi",
            "city_active_shops_lookup",
            "shop_details",
            "shop_address",
            "reviewed_shop_address_shop_id",
        ),
        (
            "oldest subscribers",
            "oldest_active_subscribers",
            "users",
            None,
            None,
        ),
        (
            "shops in each category",
            "shop_category_mapping_listing",
            "shop_details",
            "categories",
            "reviewed_shop_details_category_id",
        ),
    ],
)
def test_domain_ontology_rules_define_canonical_retrieval_anchors(
    question, term, canonical_table, related_table, relationship
):
    rule = next(
        item for item in find_relevant_rules(question)
        if item.get("term") == f"ontology_{term}"
    )
    mapping = rule["entity_resolution"]

    assert mapping["canonical_table"] == canonical_table
    if related_table is None:
        assert mapping["related_entities"] == []
        assert mapping["temporal_column"] == "subscriber_startDate"
        assert {
            (item["column"], item["operator"], item.get("value"))
            for item in mapping["filters"]
        } == {
            ("subscriber_status", "=", 1),
            ("plan_id", "IS NOT NULL", None),
            ("plan_id", "!=", 0),
            ("subscriber_startDate", "IS NOT NULL", None),
        }
        assert mapping["result_policy"] == {
            "limit": 5,
            "order_by": [{
                "table": "users",
                "column": "subscriber_startDate",
                "direction": "ASC",
            }],
        }
    else:
        assert mapping["related_entities"] == [{
            "entity": "shop_address" if related_table == "shop_address" else "category",
            "table": related_table,
            "relationship": relationship,
            "required": True,
        }]
