"""Run the model-facing child-aggregate benchmark without executing result SQL.

From ``backend``:
    python -m app.tests.run_isolated_child_plan_eval
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.agent.nodes.generate_query import generate_query
from app.agent.nodes.schema_grounding import (
    get_cached_relationships,
    get_cached_schema,
    refresh_schema_cache,
)
from app.agent.nodes.safety_check import run_safety_check
from app.db.mysql_client import close_mysql_connection, connect_to_mysql
from app.tests.plan_eval import score_generated_plan

logger = logging.getLogger(__name__)
EVALUATION_SET = Path(__file__).with_name("isolated_child_plan_eval.json")


async def evaluate_case(
    case: dict[str, Any],
    schema_context: dict[str, Any],
    relationships: list[dict[str, Any]],
    *,
    plan_generator: Callable[..., Awaitable[dict[str, Any]]] = generate_query,
    safety_validator: Callable[..., dict[str, Any]] = run_safety_check,
) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {
        "question": case["question"],
        "passed": False,
        "missing_requirements": [],
        "error": None,
    }
    try:
        plan = await plan_generator(
            rewritten_question=case["question"],
            original_question=case["question"],
            schema_context=schema_context,
            matched_business_rules=[],
            relationships=relationships,
        )
        result["query_plan"] = plan
        result["answerable"] = plan.get("answerable", True)
        result["plan_features"] = {
            feature: plan.get(feature, [])
            for feature in case.get("required_plan_features", [])
        }
        score = score_generated_plan(case, plan)
        result["missing_requirements"] = score["missing_requirements"]
        if score["passed"]:
            safety_result = safety_validator(
                plan,
                valid_tables=list(schema_context),
                schema_context=schema_context,
                relationships=relationships,
            )
            result["compiled_sql"] = safety_result.get("sql")
            result["passed"] = True
    except Exception as exc:
        logger.exception("Child-operation evaluation case failed: %s", case["question"])
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


async def run_evaluation(output_path: Path) -> dict[str, Any]:
    catalog = json.loads(EVALUATION_SET.read_text(encoding="utf-8"))
    await connect_to_mysql()
    try:
        await refresh_schema_cache()
        schema_context = get_cached_schema()
        relationships = get_cached_relationships()
        results = []
        for index, case in enumerate(catalog["cases"], start=1):
            logger.info("Evaluating child plan %d/%d", index, len(catalog["cases"]))
            results.append(
                await evaluate_case(case, schema_context, relationships)
            )
        report = {
            "evaluation_set": EVALUATION_SET.name,
            "total": len(results),
            "passed": sum(result["passed"] for result in results),
            "failed": sum(not result["passed"] for result in results),
            "results": results,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return report
    finally:
        await close_mysql_connection()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("isolated_child_plan_eval_results.json"),
        help="JSON report destination.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(run_evaluation(args.output.resolve()))
    print(
        f"Child plan evaluation: {report['passed']}/{report['total']} passed. "
        f"Report: {args.output.resolve()}"
    )


if __name__ == "__main__":
    main()
