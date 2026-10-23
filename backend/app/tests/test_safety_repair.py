import asyncio
import json

import pytest

from app.agent import graph
from app.agent.nodes import generate_query
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    UnanswerableQuestionError,
)
from app.agent.nodes.safety_check import run_safety_check
from app.agent.nodes import run_self_fix
from app.agent.nodes.run_self_fix import QueryExecutionError


SCHEMA = {
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "rating", "data_type": "decimal", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
        {"column": "status", "data_type": "tinyint", "key": ""},
    ],
}
RELATIONSHIPS = [{
    "constraint_name": "reviews_shop_fk",
    "table": "reviews",
    "referenced_table": "shop_details",
    "cardinality": "many_to_one",
    "verification": "declared_foreign_key",
    "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
}]
RELATIONSHIP_REF = {
    "constraint_name": "reviews_shop_fk",
    "table": "reviews",
    "referenced_table": "shop_details",
}


def _query(order_by):
    return {
        "answerable": True,
        "reasoning": "Rank shops by average review score.",
        "table": "reviews",
        "joins": [{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": RELATIONSHIP_REF,
        }],
        "select_items": [
            {
                "table": "shop_details",
                "column": "id",
                "aggregate": None,
                "alias": "shop_id",
            },
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "alias": "shop_name",
            },
            {
                "table": "reviews",
                "column": "rating",
                "aggregate": "AVG",
                "alias": "avg_rating",
            },
        ],
        "where_expression": {
            "table": "shop_details",
            "column": "status",
            "operator": "=",
            "value": 1,
        },
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": order_by,
        "limit": 5,
    }


class FakeGenerator:
    def __init__(self, repaired_query):
        self.repaired_query = repaired_query
        self.calls = []

    async def call_agent_json(self, role, messages, temperature=0.0):
        self.calls.append((role, messages, temperature))
        return self.repaired_query


def _state(query, question):
    return {
        "generated_query": query,
        "schema_context": SCHEMA,
        "schema_relationships": RELATIONSHIPS,
        "matched_business_rules": [],
        "classifier_result": {
            "original_question": question,
            "rewritten_question": question,
        },
        "escalated": False,
    }


@pytest.mark.parametrize(
    "question",
    [
        "Rank shops by average review score in Nagercoil.",
        "Sort the stores in Kochi by mean customer rating.",
        "Which shops near Thrissur have the best average ratings?",
    ],
)
def test_order_by_safety_rejection_gets_one_gemini_repair(monkeypatch, question):
    rejection = (
        "ORDER BY in a grouped or aggregate query must use a GROUP BY column "
        "or a selected alias."
    )
    generator = FakeGenerator(_query([
        {"column": "avg_rating", "direction": "DESC"},
    ]))
    monkeypatch.setattr(generate_query, "get_llm_service", lambda: generator)
    rejected_query = _query([
        {"table": "reviews", "column": "rating", "direction": "DESC"},
    ])
    state = _state(rejected_query, question)

    result = asyncio.run(graph.node_safety_check(state))

    assert len(generator.calls) == 1
    role, messages, temperature = generator.calls[0]
    assert role == "QUERY_GENERATOR"
    assert temperature == 0.0
    assert rejection in messages[-1]["content"]
    assert json.loads(messages[-2]["content"]) == rejected_query
    assert result["escalated"] is False
    assert result["safety_repair_attempts"] == 1
    assert result["safety_violation_reason"] is None
    assert "ORDER BY `avg_rating` DESC" in result["safety_check_result"]["sql"]


def test_blocking_sensitive_column_rejection_does_not_call_repair(monkeypatch):
    schema = {
        "users": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "password", "data_type": "varchar", "key": ""},
        ]
    }
    state = {
        "generated_query": {
            "table": "users",
            "select_items": [{
                "table": "users",
                "column": "password",
                "aggregate": None,
                "alias": "password",
            }],
        },
        "schema_context": schema,
        "schema_relationships": [],
        "classifier_result": {
            "original_question": "show user passwords",
            "rewritten_question": "show user passwords",
        },
        "escalated": False,
    }

    async def unexpected_repair(*args, **kwargs):
        raise AssertionError("blocking safety violation must not be retried")

    monkeypatch.setattr(graph, "repair_query_after_safety_rejection", unexpected_repair)

    result = asyncio.run(graph.node_safety_check(state))

    assert result["escalated"] is True
    assert result["safety_repair_attempts"] == 0
    assert result["escalation_info"]["attempts_made"] == 1
    assert "sensitive credential column" in result["safety_violation_reason"]


def test_repair_answerable_false_raises_unanswerable(monkeypatch):
    generator = FakeGenerator({
        "answerable": False,
        "reasoning": "The schema has no verified products-to-shop relationship.",
        "unanswerable_evidence": {
            "missing_tables": ["products"],
            "missing_columns": [],
            "missing_relationships": [],
        },
    })
    monkeypatch.setattr(generate_query, "get_llm_service", lambda: generator)

    with pytest.raises(UnanswerableQuestionError) as error:
        asyncio.run(generate_query.repair_query_after_safety_rejection(
            _query([]),
            "ORDER BY error",
            "Show products at the top-rated shop.",
            SCHEMA,
            [],
            RELATIONSHIPS,
            "Show products at the top-rated shop.",
        ))

    assert error.value.internal_reasoning == (
        "The schema has no verified products-to-shop relationship."
    )
    assert len(generator.calls) == 1


def test_node_escalates_when_repair_response_is_unanswerable(monkeypatch):
    generator = FakeGenerator({
        "answerable": False,
        "reasoning": "The relationship needed to answer is unavailable.",
        "unanswerable_evidence": {
            "missing_tables": ["products"],
            "missing_columns": [],
            "missing_relationships": [],
        },
    })
    monkeypatch.setattr(generate_query, "get_llm_service", lambda: generator)
    state = _state(
        _query([{"table": "reviews", "column": "rating", "direction": "DESC"}]),
        "Rank shops by average review score in Nagercoil.",
    )

    result = asyncio.run(graph.node_safety_check(state))

    assert result["escalated"] is True
    assert result["escalation_info"]["attempts_made"] == 2
    assert "unavailable" in result["escalation_info"]["reason"]
    assert "couldn't find the data" in result["unanswerable_reasoning"]


@pytest.mark.parametrize(
    ("repair_error", "reason_fragment"),
    [
        (
            UnanswerableQuestionError(
                "I couldn't answer this safely.",
                internal_reasoning="No verified relationship.",
            ),
            "No verified relationship.",
        ),
        (QueryGenerationError("Repaired plan failed validation."), "failed validation"),
    ],
)
def test_repair_errors_escalate_without_crashing(
    monkeypatch, repair_error, reason_fragment
):
    state = _state(
        _query([{"table": "reviews", "column": "rating", "direction": "DESC"}]),
        "Rank shops by average review score in Nagercoil.",
    )

    async def fail_repair(*args, **kwargs):
        raise repair_error

    monkeypatch.setattr(graph, "repair_query_after_safety_rejection", fail_repair)

    result = asyncio.run(graph.node_safety_check(state))

    assert result["escalated"] is True
    expected_repairs = 2 if isinstance(repair_error, QueryGenerationError) else 1
    assert result["escalation_info"]["attempts_made"] == 1 + expected_repairs
    assert result["escalation_info"]["repair_attempts"] == expected_repairs
    assert reason_fragment in result["escalation_info"]["reason"]
    if isinstance(repair_error, UnanswerableQuestionError):
        assert result["unanswerable_reasoning"] == repair_error.reasoning


def test_repair_validation_flags_match_normal_generation(monkeypatch):
    generator = FakeGenerator(_query([
        {"column": "avg_rating", "direction": "DESC"},
    ]))
    monkeypatch.setattr(generate_query, "get_llm_service", lambda: generator)
    original_validator = generate_query._validate_llm_output
    validator_calls = []

    def capture_validator(*args, **kwargs):
        validator_calls.append((args, kwargs))
        return original_validator(*args, **kwargs)

    monkeypatch.setattr(generate_query, "_validate_llm_output", capture_validator)

    repaired = asyncio.run(generate_query.repair_query_after_safety_rejection(
        _query([{"table": "reviews", "column": "rating", "direction": "DESC"}]),
        "ORDER BY error",
        "Rank shops by average review score in Nagercoil.",
        SCHEMA,
        [],
        RELATIONSHIPS,
        "Rank shops by average review score in Nagercoil.",
    ))

    assert repaired["table"] == "reviews"
    assert len(validator_calls) == 1
    _, kwargs = validator_calls[0]
    assert kwargs == {
        "attempt_number": 1,
        "reject_offer_heading_as_shop_type": True,
        "original_question": "Rank shops by average review score in Nagercoil.",
        "catalog_filters": None,
        "resolved_temporal_constraints": None,
        "current_state_filters": None,
    }


def test_execution_error_is_fed_to_a_validated_repair(monkeypatch):
    error_text = "Query failed: Unknown column 'wrong_name' in 'field list'"
    repaired_query = _query([
        {"column": "avg_rating", "direction": "DESC"},
    ])
    generator = FakeGenerator(repaired_query)
    monkeypatch.setattr(generate_query, "get_llm_service", lambda: generator)
    execution_calls = []

    async def fake_run_query(validated_query):
        execution_calls.append(validated_query)
        if len(execution_calls) == 1:
            raise QueryExecutionError(error_text, mysql_code=1054)
        return {
            "results": [{"shop_id": 1, "shop_name": "A Shop", "avg_rating": 4.5}],
            "columns": ["shop_id", "shop_name", "avg_rating"],
            "count": 1,
            "row_count": 1,
            "truncated": False,
            "row_limit": 200,
            "no_data_reason": None,
            "matching_row_count": None,
            "tie_at_limit": False,
            "column_formats": {},
            "suggestions": [],
        }

    monkeypatch.setattr(graph, "run_query", fake_run_query)
    query = repaired_query
    safety_result = run_safety_check(query, list(SCHEMA), SCHEMA, RELATIONSHIPS)
    state = _state(query, "Rank shops by average review score.")
    state["safety_check_result"] = safety_result

    result = asyncio.run(graph.node_execute(state))

    assert result["escalated"] is False
    assert result["execution_result"]["results"][0]["shop_name"] == "A Shop"
    assert len(generator.calls) == 1
    assert error_text in generator.calls[0][1][-1]["content"]
    assert len(execution_calls) == 2
    assert result["diagnostics"]["execution_repairs"][0]["status"] == "validated"
    assert result["generated_query"]["table"] == repaired_query["table"]
    assert result["safety_check_result"]["sql"] == execution_calls[1]["sql"]
