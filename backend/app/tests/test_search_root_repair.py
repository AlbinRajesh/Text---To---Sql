"""Search-root shape errors use the bounded safety-repair loop."""

import asyncio

import pytest

from app.agent import graph
from app.agent.nodes import generate_query
from app.agent.nodes.safety_check import SafetyViolationError


SCHEMA = {
    "shop_details": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "name", "data_type": "varchar", "key": ""},
        {"column": "status", "data_type": "tinyint", "key": ""},
        {"column": "subcategory_id", "data_type": "int", "key": ""},
    ],
    "offer": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "shop_id", "data_type": "int", "key": ""},
        {"column": "offer_heading", "data_type": "text", "key": ""},
    ],
    "subcategories": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "subcategoryname", "data_type": "varchar", "key": ""},
    ],
    "reviews": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "shop_id", "data_type": "int", "key": ""},
        {"column": "rating", "data_type": "decimal", "key": ""},
    ],
}

RELATIONSHIPS = [
    {
        "constraint_name": "reviewed_offer_shop_id",
        "table": "offer",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_shop_details_subcategory_id",
        "table": "shop_details",
        "referenced_table": "subcategories",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "subcategory_id", "referenced_column": "id"}],
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


class FakeGenerator:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def call_agent_json(self, role, messages, temperature=0.0):
        self.calls.append(messages)
        assert role == "QUERY_GENERATOR"
        assert temperature == 0.0
        return self.response


def _search_plan(table, column, term):
    return {
        "answerable": True,
        "reasoning": "Search configured shop text fields.",
        "table": table,
        "joins": [],
        "select_items": [{
            "table": table,
            "column": column,
            "aggregate": None,
            "alias": "matched_value",
        }],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "search": {"term": term, "scope": "shops"},
    }


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
    ("question", "wrong_table", "wrong_column", "term"),
    [
        ("Where can I find fish meals?", "offer", "offer_heading", "fish meals"),
        ("Which places sell fish dishes?", "subcategories", "subcategoryname", "fish dishes"),
        ("Locate shops offering grilled fish.", "reviews", "rating", "grilled fish"),
    ],
)
def test_wrong_search_root_is_repaired_once(
    monkeypatch, question, wrong_table, wrong_column, term
):
    wrong_plan = _search_plan(wrong_table, wrong_column, term)
    initial_generator = FakeGenerator(wrong_plan)
    monkeypatch.setattr(
        generate_query, "get_llm_service", lambda: initial_generator
    )
    generated = asyncio.run(generate_query.generate_query(
        question, SCHEMA, [], RELATIONSHIPS, question
    ))
    assert generated["table"] == wrong_table
    assert len(initial_generator.calls) == 1
    assert "base table MUST" in "\n".join(
        message["content"] for message in initial_generator.calls[0]
    )

    repaired = _search_plan("shop_details", "name", term)
    repair_generator = FakeGenerator(repaired)
    monkeypatch.setattr(
        generate_query, "get_llm_service", lambda: repair_generator
    )

    result = asyncio.run(graph.node_safety_check(
        _state(generated, question)
    ))

    assert len(repair_generator.calls) == 1
    assert "requires base table 'shop_details'" in (
        repair_generator.calls[0][-1]["content"]
    )
    assert result["escalated"] is False
    assert result["safety_repair_attempts"] == 1
    assert result["generated_query"]["table"] == "shop_details"
    assert "`shop_details`.`status` = %s" not in result["safety_check_result"]["sql"]
    assert result["generated_query"]["where_expression"] is None


def test_second_wrong_search_root_escalates_cleanly(monkeypatch):
    wrong = _search_plan("offer", "offer_heading", "fish meals")
    initial_generator = FakeGenerator(wrong)
    monkeypatch.setattr(
        generate_query, "get_llm_service", lambda: initial_generator
    )
    generated = asyncio.run(generate_query.generate_query(
        "Where can I find fish meals?", SCHEMA, [], RELATIONSHIPS,
        "Where can I find fish meals?",
    ))
    repair_generator = FakeGenerator(wrong)
    monkeypatch.setattr(
        generate_query, "get_llm_service", lambda: repair_generator
    )

    result = asyncio.run(graph.node_safety_check(
        _state(generated, "Where can I find fish meals?")
    ))

    assert len(initial_generator.calls) == 1
    assert len(repair_generator.calls) == 2
    assert "requires base table 'shop_details'" in (
        repair_generator.calls[0][-1]["content"]
    )
    assert result["escalated"] is True
    assert result["safety_repair_attempts"] == 2
    assert result["escalation_info"]["attempts_made"] == 3
    assert result["escalation_info"]["repair_attempts"] == 2
    assert "requires base table 'shop_details'" in result["escalation_info"]["reason"]


def test_wrong_search_root_has_repairable_rule_id():
    from app.agent.nodes.safety_check import REPAIRABLE_SAFETY_RULES, run_safety_check

    query = _search_plan("offer", "offer_heading", "fish meals")
    with pytest.raises(SafetyViolationError) as error:
        run_safety_check(query, list(SCHEMA), SCHEMA, RELATIONSHIPS)

    assert error.value.rule_id == "search_wrong_base_table"
    assert error.value.rule_id in REPAIRABLE_SAFETY_RULES
