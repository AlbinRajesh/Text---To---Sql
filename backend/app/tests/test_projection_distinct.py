import pytest

from app.agent.nodes import generate_query as gq
from app.agent.nodes.safety_check import (
    SafetyViolationError,
    _ast_verify,
    run_safety_check,
)


SCHEMA = {
    "plans": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "name", "data_type": "varchar"},
        {"column": "price", "data_type": "decimal"},
    ],
}


def _plan(*, distinct=True, select_items=None, group_by=None):
    return {
        "answerable": True,
        "reasoning": "Return unique projected plan rows.",
        "table": "plans",
        "joins": [],
        "select_items": select_items or [{
            "table": "plans",
            "column": "name",
            "aggregate": None,
            "distinct": False,
            "alias": "plan_name",
        }],
        "distinct": distinct,
        "where_expression": None,
        "group_by": group_by or [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
        "limit": None,
    }


def test_projection_distinct_compiles_and_ast_matches_validated_plan():
    validated = gq._validate_llm_output(_plan(), SCHEMA)

    compiled = run_safety_check(validated, list(SCHEMA), SCHEMA)

    assert "SELECT /*+ MAX_EXECUTION_TIME(" in compiled["sql"]
    assert " */ DISTINCT `plans`.`name` AS `plan_name`" in compiled["sql"]
    with pytest.raises(
        SafetyViolationError,
        match="projection DISTINCT does not match",
    ):
        _ast_verify(
            compiled["sql"],
            SCHEMA,
            expected_query={**validated, "distinct": False},
        )


@pytest.mark.parametrize(
    "plan",
    [
        _plan(distinct="yes"),
        _plan(
            distinct=True,
            select_items=[{
                "table": "plans",
                "column": "price",
                "aggregate": "SUM",
                "distinct": False,
                "alias": "total_price",
            }],
        ),
        _plan(distinct=True, group_by=[{"table": "plans", "column": "name"}]),
    ],
)
def test_projection_distinct_rejects_invalid_shapes(plan):
    with pytest.raises(gq.QueryGenerationError):
        gq._validate_llm_output(plan, SCHEMA)


def test_safety_compiler_rejects_untyped_projection_distinct():
    plan = {
        "table": "plans",
        "joins": [],
        "select_items": [{
            "table": "plans",
            "column": "name",
            "aggregate": None,
            "alias": "plan_name",
        }],
        "distinct": True,
        "group_by": [{"table": "plans", "column": "name"}],
        "having_expression": None,
        "count_groups": False,
        "where_expression": None,
        "order_by": [],
    }

    with pytest.raises(
        SafetyViolationError,
        match="Projection DISTINCT is supported only",
    ):
        run_safety_check(plan, list(SCHEMA), SCHEMA)
