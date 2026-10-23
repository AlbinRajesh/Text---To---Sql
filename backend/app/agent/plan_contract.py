"""Shared structured-query plan contract used by generation and safety layers."""

RANKING_INTENT_POLICIES = {
    "none": {
        "requires_order_by": False,
        "requires_positive_limit": False,
        "returns_all_optimal_rows": False,
        "requires_stable_tie_break": False,
    },
    "single_best_with_ties": {
        "requires_order_by": True,
        "requires_positive_limit": False,
        "returns_all_optimal_rows": True,
        "requires_stable_tie_break": False,
    },
    "top_n": {
        "requires_order_by": True,
        "requires_positive_limit": True,
        "returns_all_optimal_rows": False,
        "requires_stable_tie_break": True,
    },
    "latest": {
        "requires_order_by": True,
        "requires_positive_limit": True,
        "returns_all_optimal_rows": False,
        "requires_stable_tie_break": True,
    },
}

RANKING_INTENTS = tuple(RANKING_INTENT_POLICIES)


def ranking_policy_for(intent: str) -> dict[str, bool]:
    try:
        return RANKING_INTENT_POLICIES[intent]
    except KeyError as exc:
        raise ValueError(f"Unsupported ranking intent: {intent!r}") from exc

QUERY_PLAN_FIELDS = {
    "answerable": "Whether the requested data can be answered from the supplied schema.",
    "reasoning": "A brief explanation of the plan or the missing evidence.",
    "unanswerable_evidence": "Exact missing tables, columns, and relationship pairs.",
    "table": "The base table for the query.",
    "joins": "Verified relationship joins to other tables.",
    "select_items": "Projected columns, registered metrics, or aggregate expressions.",
    "where_expression": "Row-level filters.",
    "exclude_entities_with_rows_in": "Direct-child exclusions compiled as NOT EXISTS.",
    "isolated_child_aggregates": (
        "Independent direct-child aggregates compiled in separate grouped subqueries "
        "to prevent join fan-out."
    ),
    "group_by": "Grouping columns and optional date buckets.",
    "having_expression": "Aggregate-level filters, including declared child metric aliases.",
    "search": "A typed text-search request in a configured scope.",
    "count_groups": "Whether to count the qualifying groups.",
    "distinct": "Whether duplicate complete projected rows should be removed.",
    "count_entity": "The entity primary key used for joined COUNT operations.",
    "order_by": "Sort expressions and directions.",
    "ranking_intent": "The answer shape: no rank, all co-optimal rows, top-N, or latest.",
    "limit": "A user-requested row limit; omit for single_best_with_ties.",
    "include_empty_groups": "Whether supported numeric summaries include empty groups.",
    "where_conditions": "Legacy compatibility input normalized into where_expression.",
    "combine_with": "Legacy compatibility operator paired with where_conditions.",
}

PLAN_FEATURE_ATTRIBUTES = {
    "exclude_entities_with_rows_in": (
        "table",
        "relationship",
        "where_expression",
    ),
    "isolated_child_aggregates": (
        "table",
        "relationship",
        "select_items",
        "where_expression",
    ),
    "isolated_child_aggregate_select_item": (
        "column",
        "aggregate",
        "distinct",
        "alias",
    ),
    "verified_relationship": (
        "constraint_name",
        "table",
        "referenced_table",
    ),
    "ranking_intent": RANKING_INTENTS,
}

PROMPT_QUERY_PLAN_FIELDS = tuple(
    field
    for field in QUERY_PLAN_FIELDS
    if field not in {"where_conditions", "combine_with"}
)

VALIDATOR_QUERY_PLAN_FIELDS = frozenset(QUERY_PLAN_FIELDS)

SAFETY_QUERY_PLAN_FIELDS = frozenset({
    "table",
    "joins",
    "select_items",
    "where_expression",
    "exclude_entities_with_rows_in",
    "isolated_child_aggregates",
    "group_by",
    "having_expression",
    "search",
    "count_groups",
    "distinct",
    "count_entity",
    "order_by",
    "ranking_intent",
    "limit",
    "include_empty_groups",
    "metric_filter_unit",
})

COMPILER_OUTPUT_FIELDS = frozenset({
    "table",
    "joins",
    "select_items",
    "where_expression",
    "group_by",
    "having_expression",
    "count_groups",
    "distinct",
    "count_entity",
    "order_by",
    "ranking_intent",
    "include_empty_groups",
    "exclude_entities_with_rows_in",
    "isolated_child_aggregates",
    "metric_filter_unit",
    "reasoning",
    "search",
    "limit",
})

CONDITIONAL_COMPILER_OUTPUT_FIELDS = frozenset({"search", "limit"})


def build_query_plan_documentation() -> str:
    """Render the shared model-facing field contract and ranking intent values."""
    rows = [
        f"- `{field}`: {QUERY_PLAN_FIELDS[field]}"
        for field in PROMPT_QUERY_PLAN_FIELDS
    ]
    intents = ", ".join(f"`{intent}`" for intent in RANKING_INTENTS)
    nested_shapes = "\n".join(
        f"- `{feature}` attributes: "
        + ", ".join(f"`{attribute}`" for attribute in attributes)
        for feature, attributes in PLAN_FEATURE_ATTRIBUTES.items()
        if feature != "ranking_intent"
    )
    return (
        "Canonical structured-plan fields (also enforced by the compiler and "
        "independent safety checker):\n"
        + "\n".join(rows)
        + "\n\nNested feature attributes derived from the compiler contract:\n"
        + nested_shapes
        + "\n\n"
        + f"Allowed `ranking_intent` values: {intents}. "
        "`single_best_with_ties` omits `limit`; the compiler returns every "
        "co-optimal row within its configured result cap. `top_n` and `latest` "
        "require a positive user-requested `limit`."
    )
