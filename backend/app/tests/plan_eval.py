"""Scoring helpers for the opt-in child-operation plan evaluation set."""

from typing import Any


def score_generated_plan(case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    missing = []
    required_features = case.get("required_plan_features", [])
    for feature in required_features:
        if not isinstance(plan.get(feature), list) or not plan[feature]:
            missing.append(f"required operation missing: {feature}")

    isolated = plan.get("isolated_child_aggregates", [])
    exclusions = plan.get("exclude_entities_with_rows_in", [])
    isolated_tables = {
        item.get("table") for item in isolated if isinstance(item, dict)
    }
    excluded_tables = {
        item.get("table") for item in exclusions if isinstance(item, dict)
    }
    for table in case.get("required_child_tables", []):
        if table not in isolated_tables:
            missing.append(f"isolated child metric missing for: {table}")
    for table in case.get("excluded_child_tables", []):
        if table not in excluded_tables:
            missing.append(f"child exclusion missing for: {table}")

    filtered_tables = {
        item.get("table")
        for item in [*isolated, *exclusions]
        if isinstance(item, dict) and item.get("where_expression") is not None
    }
    for table in case.get("filtered_child_tables", []):
        if table not in filtered_tables:
            missing.append(f"child-only where_expression missing for: {table}")
    if case.get("requires_multiple_metrics_in_child"):
        if not any(
            isinstance(item, dict)
            and isinstance(item.get("select_items"), list)
            and len(item["select_items"]) >= 2
            for item in isolated
        ):
            missing.append("multiple metrics were not combined in one child operation")
    if case.get("requires_metric_threshold"):
        declared_aliases = {
            item.get("alias")
            for aggregate in isolated
            if isinstance(aggregate, dict)
            for item in aggregate.get("select_items", [])
            if isinstance(item, dict)
        }

        def has_declared_metric_predicate(expression: Any) -> bool:
            if not isinstance(expression, dict):
                return False
            if expression.get("metric_alias") in declared_aliases:
                return True
            return any(
                has_declared_metric_predicate(child)
                for child in expression.get("conditions", [])
            )

        if not has_declared_metric_predicate(plan.get("having_expression")):
            missing.append("HAVING must filter a declared isolated metric alias")

    return {"passed": not missing, "missing_requirements": missing}
