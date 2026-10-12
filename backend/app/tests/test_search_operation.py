import asyncio
import logging
import re

import pytest

from app.agent import search_config
from app.agent import graph
from app.agent.nodes import generate_query, grade_sample, schema_grounding
from app.agent.nodes.generate_query import QueryGenerationError
from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check
from app.agent.nodes.schema_grounding import SchemaGroundingError


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
]


def _plan(term, scope="shops"):
    return {
        "answerable": True,
        "reasoning": "Find shops matching the requested text.",
        "table": "shop_details",
        "joins": [],
        "select_items": [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "alias": "shop_name",
            },
        ],
        "where_expression": None,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "search": {"term": term, "scope": scope},
    }


@pytest.mark.parametrize(
    ("term", "expected_variants"),
    [
        ("fish meal", ["fish meal"]),
        ("fish meals", ["fish meals", "fish meal"]),
        ("shoes", ["shoes", "shoe"]),
        ("pickles", ["pickles", "pickle"]),
        ("boxes", ["boxes", "box"]),
        # The heuristic strips a trailing "s" even when it widens the word:
        # class matches "clas", and glasses matches "glasse".
        ("class", ["class", "clas"]),
        ("glasses", ["glasses", "glasse"]),
    ],
)
def test_search_plural_variants_compile_for_all_configured_fields(
    term, expected_variants
):
    query = _plan(term)

    result = run_safety_check(
        query, list(SCHEMA), SCHEMA, RELATIONSHIPS
    )

    assert "LOWER(`shop_details`.`name`) LIKE %s ESCAPE '!'" in result["sql"]
    assert "EXISTS (SELECT 1 FROM `offer`" in result["sql"]
    assert "`offer`.`offer_heading`" in result["sql"]
    assert "EXISTS (SELECT 1 FROM `subcategories`" in result["sql"]
    assert "`subcategories`.`subcategoryname`" in result["sql"]
    assert "`service`" not in result["sql"]
    assert all(
        field["table"] != "service"
        for field in search_config.SEARCHABLE_COLUMNS["shops"]
    )
    expected_patterns = [
        f"%{variant.lower()}%"
        for _field in search_config.SEARCHABLE_COLUMNS["shops"]
        for variant in expected_variants
    ]
    assert result["params"][:-1] == expected_patterns
    assert result["params"][-1] == 201


def test_service_name_remains_disabled_pending_verified_relationship():
    service_field = next(
        entry
        for entry in search_config.DISABLED_SEARCHABLE_COLUMNS
        if entry["column"] == "service_name"
    )

    assert service_field["enabled"] is False
    assert "offer.service_id -> service.id" in service_field["reason"]


def test_search_values_are_parameterized_and_like_wildcards_escaped():
    query = _plan("Fish%_!")

    result = run_safety_check(
        query, list(SCHEMA), SCHEMA, RELATIONSHIPS
    )

    assert "Fish%_!" not in result["sql"]
    assert result["params"][:-1] == [
        "%fish!%!_!!%" for _ in search_config.SEARCHABLE_COLUMNS["shops"]
    ]
    assert result["params"][-1] == 201


def test_normal_generator_path_adds_active_shop_filter_before_search(monkeypatch):
    class FakeLLM:
        async def call_agent_json(self, role, messages, temperature=0.0):
            assert role == "QUERY_GENERATOR"
            return _plan("fish meals")

    monkeypatch.setattr(generate_query, "get_llm_service", lambda: FakeLLM())
    raw_plan = _plan("fish meals")

    generated = asyncio.run(generate_query.generate_query(
        "who sells fish meals?",
        SCHEMA,
        [],
        RELATIONSHIPS,
        "who sells fish meals?",
    ))
    result = run_safety_check(generated, list(SCHEMA), SCHEMA, RELATIONSHIPS)

    assert generated["where_expression"] == {
        "table": "shop_details",
        "column": "status",
        "operator": "=",
        "value": 1,
    }
    assert "`shop_details`.`status` = %s" in result["sql"]
    assert result["params"][0] == 1
    assert result["params"][1:-1] == [
        f"%{variant.lower()}%"
        for _field in search_config.SEARCHABLE_COLUMNS["shops"]
        for variant in ("fish meals", "fish meal")
    ]
    assert result["params"][-1] == 201
    assert raw_plan["where_expression"] is None


def test_search_with_group_by_and_limit_preserves_parameter_order():
    query = _plan("boxes")
    query["where_expression"] = {
        "table": "shop_details",
        "column": "status",
        "operator": "=",
        "value": 1,
    }
    query["group_by"] = [{"table": "shop_details", "column": "name"}]
    query["limit"] = 7

    result = run_safety_check(
        query, list(SCHEMA), SCHEMA, RELATIONSHIPS
    )

    assert "GROUP BY `shop_details`.`name`" in result["sql"]
    assert result["sql"].endswith("LIMIT %s")
    assert result["params"] == [
        1,
        *[
            f"%{variant.lower()}%"
            for _field in search_config.SEARCHABLE_COLUMNS["shops"]
            for variant in ("boxes", "box")
        ],
        8,
    ]


@pytest.mark.parametrize(
    "question",
    [
        "Where can I find fish meals?",
        "Which places sell fish dishes?",
        "Locate shops offering grilled fish.",
    ],
)
def test_schema_grader_always_receives_minimal_search_context(
    monkeypatch, caplog, question
):
    caplog.set_level(logging.INFO, logger=grade_sample.__name__)
    schema = {
        **SCHEMA,
        "bookings": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "user_id", "data_type": "int", "key": ""},
            {"column": "final_amount", "data_type": "decimal", "key": ""},
        ],
    }
    calls = {"retrieve": 0, "grade": 0}

    async def retrieve(original_question, top_k=6):
        calls["retrieve"] += 1
        return ["bookings"]

    async def grade(original_question, rewritten_question, schema_text, relationships):
        calls["grade"] += 1
        assert "shop_details: id, name, status, subcategory_id" in schema_text
        assert "offer: id, shop_id, offer_heading" in schema_text
        assert "subcategories: id, subcategoryname" in schema_text
        assert "bookings: id, user_id, final_amount" in schema_text
        assert {
            edge["constraint_name"] for edge in relationships
        } >= {
            "reviewed_offer_shop_id",
            "reviewed_shop_details_subcategory_id",
        }
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(
        grade_sample, "get_cached_relationships", lambda: RELATIONSHIPS
    )
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)

    result = asyncio.run(grade_sample.grade_schema_sufficiency(question))

    assert result["sufficient"] is True
    assert result["attempts"] == 1
    assert calls == {"retrieve": 1, "grade": 1}
    assert {"shop_details", "offer", "subcategories"} <= set(result["schema"])
    prompt_lengths = re.search(
        r"Schema grader prompt characters before configured search context=(\d+) "
        r"after=(\d+)",
        caplog.text,
    )
    assert prompt_lengths is not None
    assert int(prompt_lengths.group(2)) > int(prompt_lengths.group(1))


def test_search_context_addition_does_not_drop_retrieved_tables_at_cap():
    retrieved_tables = {
        f"retrieved_{index}": [
            {"column": "id", "data_type": "int", "key": "PRI"}
        ]
        for index in range(24)
    }
    full_schema = {
        **retrieved_tables,
        **SCHEMA,
    }

    augmented, _ = grade_sample._add_configured_search_context(
        retrieved_tables, full_schema, [], RELATIONSHIPS
    )

    assert set(retrieved_tables) <= set(augmented)
    assert {"shop_details", "offer", "subcategories"} <= set(augmented)


def test_schema_grader_prompt_lists_configured_search_fields():
    prompt = grade_sample._build_schema_grader_prompt(
        "How many bookings?",
        "How many bookings?",
        "bookings: id",
        [],
    )

    assert "Configured text-search fields:" in prompt
    for fields in search_config.SEARCHABLE_COLUMNS.values():
        for field in fields:
            assert f"{field['table']}.{field['column']}" in prompt


def test_schema_grader_prompt_fields_follow_search_config(monkeypatch):
    question = "How many bookings?"
    original_prompt = grade_sample._build_schema_grader_prompt(
        question, question, "bookings: id", []
    )
    fields = {
        **search_config.SEARCHABLE_COLUMNS,
        "shops": (
            *search_config.SEARCHABLE_COLUMNS["shops"],
            {
                "table": "custom_search_table",
                "column": "custom_search_text",
                "relationship": None,
            },
        ),
    }
    monkeypatch.setattr(grade_sample, "SEARCHABLE_COLUMNS", fields)

    changed_prompt = grade_sample._build_schema_grader_prompt(
        question, question, "bookings: id", []
    )

    assert "custom_search_table.custom_search_text" not in original_prompt
    assert "custom_search_table.custom_search_text" in changed_prompt


def test_schema_grader_prompt_preserves_verified_relationship_rule():
    prompt = grade_sample._build_schema_grader_prompt(
        "How many bookings?",
        "How many bookings?",
        "bookings: id",
        [],
    )

    assert "Verified table relationships (only these may be joined):" in prompt
    assert "For every other question, use only relationships verified in the catalog below." in prompt
    assert "Judge sufficiency only against exact tables, columns, and verified relationships shown above." in prompt


def test_non_search_prompt_changes_only_by_configured_search_section(monkeypatch):
    question = "How many bookings?"
    prompt_with_config = grade_sample._build_schema_grader_prompt(
        question, question, "bookings: id", []
    )
    configured_lines = "\n".join(
        f"{scope}: " + ", ".join(
            f"{field['table']}.{field['column']}" for field in fields
        )
        for scope, fields in search_config.SEARCHABLE_COLUMNS.items()
    )

    monkeypatch.setattr(grade_sample, "SEARCHABLE_COLUMNS", {})
    prompt_without_fields = grade_sample._build_schema_grader_prompt(
        question, question, "bookings: id", []
    )
    marker = "Configured text-search fields:\n\n\n"
    assert marker in prompt_without_fields
    expected_prompt = prompt_without_fields.replace(
        marker,
        f"Configured text-search fields:\n{configured_lines}\n\n",
        1,
    )

    assert prompt_with_config == expected_prompt


def test_graded_search_schema_is_passed_to_query_generator(monkeypatch):
    schema = {
        **SCHEMA,
        "bookings": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "user_id", "data_type": "int", "key": ""},
            {"column": "final_amount", "data_type": "decimal", "key": ""},
        ],
    }
    question = "Where can I find fish meals?"

    async def retrieve(original_question, top_k=6):
        return ["bookings"]

    async def grade(original_question, rewritten_question, schema_text, relationships):
        return True, []

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(
        grade_sample, "get_cached_relationships", lambda: RELATIONSHIPS
    )
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)

    state = {
        "classifier_result": {
            "original_question": question,
            "rewritten_question": question,
        },
        "matched_business_rules": [],
    }
    graded = asyncio.run(graph.node_grade_schema(state))

    received = {}

    async def capture_generate_query(
        rewritten_question,
        schema_context,
        matched_business_rules,
        relationships,
        original_question,
        request_id=None,
    ):
        received["schema_context"] = schema_context
        received["relationships"] = relationships
        return {"table": "shop_details"}

    monkeypatch.setattr(graph, "generate_query", capture_generate_query)
    generated = asyncio.run(graph.node_generate_query(graded))
    context = received["schema_context"]

    assert generated["answerable"] is True
    assert {"shop_details", "offer", "subcategories"} <= set(context)
    assert any(
        column["column"] == "status" for column in context["shop_details"]
    )
    assert {
        edge["constraint_name"] for edge in received["relationships"]
    } >= {
        "reviewed_offer_shop_id",
        "reviewed_shop_details_subcategory_id",
    }


def test_schema_grader_logs_missing_items_for_each_attempt(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger=grade_sample.__name__)
    calls = {"grade": 0}

    async def retrieve(original_question, top_k=6):
        return ["bookings"]

    async def grade(original_question, rewritten_question, schema_text, relationships):
        calls["grade"] += 1
        return calls["grade"] == 3, [{
            "type": "column",
            "detail": f"missing_column_{calls['grade']}",
            "tables": ["bookings"],
        }]

    schema = {
        **SCHEMA,
        "bookings": [
            {"column": "id", "data_type": "int", "key": "PRI"},
            {"column": "user_id", "data_type": "int", "key": ""},
            {"column": "final_amount", "data_type": "decimal", "key": ""},
        ],
    }
    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: schema)
    monkeypatch.setattr(
        grade_sample, "get_cached_relationships", lambda: RELATIONSHIPS
    )
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", grade)

    result = asyncio.run(
        grade_sample.grade_schema_sufficiency("How many bookings?")
    )

    assert calls["grade"] == 3
    assert result["sufficient"] is True
    assert result["attempts"] == 3
    for attempt in range(1, 4):
        assert (
            f"Schema grader missing items on attempt {attempt}: "
            f"['missing_column_{attempt}']"
        ) in caplog.text


def test_search_term_over_limit_is_rejected():
    with pytest.raises(QueryGenerationError, match="100 characters"):
        generate_query._validate_llm_output(
            _plan("x" * 101),
            SCHEMA,
            RELATIONSHIPS,
            "find shops",
        )


def test_generator_accepts_only_term_and_scope_for_search():
    validated = generate_query._validate_llm_output(
        _plan("fish meals"), SCHEMA, RELATIONSHIPS, "who sells fish meals?"
    )

    assert validated["search"] == {"term": "fish meals", "scope": "shops"}
    assert set(validated["search"]) == {"term", "scope"}

    malformed = _plan("fish meals")
    malformed["search"]["column"] = "offer_heading"
    with pytest.raises(QueryGenerationError, match="exactly 'term' and 'scope'"):
        generate_query._validate_llm_output(
            malformed, SCHEMA, RELATIONSHIPS, "who sells fish meals?"
        )


def test_model_written_subquery_is_rejected_before_compilation():
    query = _plan("fish meals")
    query["where_expression"] = {
        "boolean": "AND",
        "conditions": [
            {
                "table": "shop_details",
                "column": "status",
                "operator": "=",
                "value": 1,
            },
            {
                "exists": {
                    "table": "offer",
                    "sql": "SELECT offer_heading FROM offer",
                }
            },
        ],
    }

    with pytest.raises(QueryGenerationError, match="subqueries are not supported"):
        generate_query._validate_llm_output(
            query, SCHEMA, RELATIONSHIPS, "who sells fish meals?"
        )

    with pytest.raises(SafetyViolationError, match="subqueries are not allowed"):
        run_safety_check(query, list(SCHEMA), SCHEMA, RELATIONSHIPS)


def test_search_cannot_be_silently_dropped_by_empty_group_compiler():
    query = _plan("fish meals")
    query["include_empty_groups"] = True

    with pytest.raises(SafetyViolationError, match="cannot be combined"):
        run_safety_check(query, list(SCHEMA), SCHEMA, RELATIONSHIPS)


def test_startup_validation_fails_for_a_bad_search_config_entry(monkeypatch):
    bad_fields = {
        "shops": (
            {
                "table": "shop_details",
                "column": "not_a_real_column",
                "relationship": None,
            },
        ),
    }
    monkeypatch.setattr(search_config, "SEARCHABLE_COLUMNS", bad_fields)
    monkeypatch.setattr(
        schema_grounding,
        "fetch_schema_from_information_schema",
        lambda: asyncio.sleep(0, result=SCHEMA),
    )
    monkeypatch.setattr(
        schema_grounding,
        "fetch_relationships_from_information_schema",
        lambda: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        schema_grounding,
        "_validated_relationships",
        lambda tables, declared: RELATIONSHIPS,
    )

    with pytest.raises(SchemaGroundingError, match="not_a_real_column"):
        asyncio.run(schema_grounding.refresh_schema_cache())
