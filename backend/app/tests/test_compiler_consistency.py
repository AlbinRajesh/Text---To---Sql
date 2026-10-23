"""Guard against drift between the normalized plan, prompt, and safety compiler."""

import ast
import asyncio
import json
from pathlib import Path

from app.agent import plan_contract
from app.agent.nodes import generate_query, safety_check
from app.agent.prompts.generate_query_prompt import SYSTEM_PROMPT
from app.tests.plan_eval import score_generated_plan
from app.tests.run_isolated_child_plan_eval import evaluate_case


def _normalized_plan_literal_keys() -> set[str]:
    tree = ast.parse(Path(generate_query.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "normalized_query"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        return {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    raise AssertionError("Could not locate the normalized query-plan output.")


def _string_get_keys(source: str, function_name: str) -> set[str]:
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    return {
        call.args[0].value
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "get"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }


def test_compiler_output_fields_are_registered_and_documented():
    validator_source = Path(generate_query.__file__).read_text(encoding="utf-8")
    compiled_fields = _normalized_plan_literal_keys()
    conditional_fields = plan_contract.CONDITIONAL_COMPILER_OUTPUT_FIELDS

    assert compiled_fields | conditional_fields == plan_contract.COMPILER_OUTPUT_FIELDS
    assert plan_contract.VALIDATOR_QUERY_PLAN_FIELDS == set(plan_contract.QUERY_PLAN_FIELDS)
    assert (
        set(plan_contract.QUERY_PLAN_FIELDS)
        - {"where_conditions", "combine_with"}
    ) <= compiled_fields | conditional_fields | {
        "answerable",
        "unanswerable_evidence",
    }
    assert plan_contract.SAFETY_QUERY_PLAN_FIELDS <= (
        set(plan_contract.QUERY_PLAN_FIELDS) | {"metric_filter_unit"}
    )

    missing_prompt_fields = [
        field for field in plan_contract.PROMPT_QUERY_PLAN_FIELDS
        if f"`{field}`" not in SYSTEM_PROMPT
    ]
    assert not missing_prompt_fields

    safety_source = Path(safety_check.__file__).read_text(encoding="utf-8")
    compiler_input_attributes = (
        _string_get_keys(validator_source, "_normalize_isolated_query_parts")
        | _string_get_keys(
            validator_source, "_verified_direct_child_relationship"
        )
    )
    expected_input_attributes = {
        attribute
        for feature, attributes in plan_contract.PLAN_FEATURE_ATTRIBUTES.items()
        if feature != "ranking_intent"
        for attribute in attributes
    }
    assert expected_input_attributes <= compiler_input_attributes

    for feature, attributes in plan_contract.PLAN_FEATURE_ATTRIBUTES.items():
        if feature == "ranking_intent":
            continue
        assert f"`{feature}`" in SYSTEM_PROMPT
        for attribute in attributes:
            assert f"`{attribute}`" in SYSTEM_PROMPT
            assert attribute in validator_source
            assert attribute in safety_source

    missing_safety_fields = [
        field for field in plan_contract.SAFETY_QUERY_PLAN_FIELDS
        if f'"{field}"' not in safety_source and f"'{field}'" not in safety_source
    ]
    assert not missing_safety_fields


def test_ranking_intents_are_shared_by_prompt_generator_and_safety_layer():
    generator_source = Path(generate_query.__file__).read_text(encoding="utf-8")
    safety_source = Path(safety_check.__file__).read_text(encoding="utf-8")
    for intent in plan_contract.RANKING_INTENTS:
        assert f"`{intent}`" in SYSTEM_PROMPT
        assert intent in generator_source
        assert intent in safety_source


def test_unseen_child_operation_evaluation_set_has_ten_diverse_cases():
    path = Path(__file__).with_name("isolated_child_plan_eval.json")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    cases = catalog["cases"]

    assert len(cases) == 10
    assert len({case["question"] for case in cases}) == len(cases)
    assert any(
        "isolated_child_aggregates" in case["required_plan_features"]
        and "exclude_entities_with_rows_in" in case["required_plan_features"]
        for case in cases
    )
    assert any(case.get("filtered_child_tables") for case in cases)
    assert any(case.get("requires_multiple_metrics_in_child") for case in cases)


def test_child_operation_eval_scorer_checks_expected_plan_features():
    case = {
        "required_plan_features": ["isolated_child_aggregates"],
        "required_child_tables": ["bookings", "reviews"],
    }
    result = score_generated_plan(case, {
        "isolated_child_aggregates": [
            {
                "table": "bookings",
                "select_items": [{"alias": "total_amount"}],
            },
            {
                "table": "reviews",
                "select_items": [{"alias": "average_rating"}],
            },
        ],
    })

    assert result == {"passed": True, "missing_requirements": []}


def test_child_operation_eval_scorer_catches_missing_behavior():
    result = score_generated_plan({
        "required_plan_features": [
            "isolated_child_aggregates",
            "exclude_entities_with_rows_in",
        ],
        "required_child_tables": ["reviews"],
        "excluded_child_tables": ["offer"],
        "filtered_child_tables": ["reviews"],
        "requires_multiple_metrics_in_child": True,
        "requires_metric_threshold": True,
    }, {
        "isolated_child_aggregates": [{
            "table": "reviews",
            "select_items": [{"alias": "average_rating"}],
        }],
        "exclude_entities_with_rows_in": [],
    })

    assert result["passed"] is False
    assert "child exclusion missing for: offer" in result["missing_requirements"]
    assert (
        "child-only where_expression missing for: reviews"
        in result["missing_requirements"]
    )
    assert (
        "multiple metrics were not combined in one child operation"
        in result["missing_requirements"]
    )
    assert (
        "HAVING must filter a declared isolated metric alias"
        in result["missing_requirements"]
    )


def test_model_behavior_eval_runs_plan_validation_without_query_execution():
    called = []

    async def fake_plan_generator(**kwargs):
        called.append(kwargs["rewritten_question"])
        return {
            "answerable": True,
            "table": "shop_details",
            "joins": [],
            "select_items": [{"table": "shop_details", "column": "name", "alias": "shop_name"}],
            "isolated_child_aggregates": [{
                "table": "reviews",
                "select_items": [{"alias": "average_rating"}],
            }],
        }

    def fake_safety_validator(plan, **kwargs):
        called.append("safety-validated")
        return {"sql": "SELECT safely compiled"}

    result = asyncio.run(evaluate_case(
        {
            "question": "For each shop, show its average review rating.",
            "required_plan_features": ["isolated_child_aggregates"],
            "required_child_tables": ["reviews"],
        },
        {"shop_details": [], "reviews": []},
        [],
        plan_generator=fake_plan_generator,
        safety_validator=fake_safety_validator,
    ))

    assert result["passed"] is True
    assert called == [
        "For each shop, show its average review rating.",
        "safety-validated",
    ]
