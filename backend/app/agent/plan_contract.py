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
    "where_expression": (
        "Row-level filters. Date comparisons may use the typed "
        "latest_recorded_date dynamic anchor or a typed max_date/min_date "
        "scalar_subquery, current_month_start/next_month_start calendar boundaries, "
        "or HOUR/TIME function filters on verified date/time columns."
    ),
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

DYNAMIC_DATE_ANCHOR = "latest_recorded_date"
DYNAMIC_DATE_ANCHOR_FIELD = "dynamic_anchor"
_DATE_DATA_TYPES = {"date", "datetime", "timestamp"}
SCALAR_DATE_SUBQUERY_OPERATIONS = {"max_date", "min_date"}
CURRENT_MONTH_BOUNDARIES = {"current_month_start", "next_month_start"}


def validate_dynamic_date_anchor(
    value: object,
    schema_context: dict,
    query_tables: set[str] | None = None,
) -> dict[str, str]:
    """Validate the one supported dynamic date value against the supplied schema."""
    if (
        not isinstance(value, dict)
        or set(value) != {DYNAMIC_DATE_ANCHOR_FIELD, "table", "column"}
        or value.get(DYNAMIC_DATE_ANCHOR_FIELD) != DYNAMIC_DATE_ANCHOR
    ):
        raise ValueError("Unsupported dynamic date anchor.")

    table, column = value.get("table"), value.get("column")
    if (
        not isinstance(table, str)
        or not isinstance(column, str)
        or table not in schema_context
        or (query_tables is not None and table not in query_tables)
    ):
        raise ValueError("Dynamic date anchor must identify a table in the query.")

    column_info = next(
        (
            item
            for item in schema_context.get(table, [])
            if isinstance(item, dict) and item.get("column") == column
        ),
        None,
    )
    data_type = (
        column_info.get("data_type", "").casefold()
        if isinstance(column_info, dict)
        and isinstance(column_info.get("data_type"), str)
        else ""
    )
    if column_info is None or data_type not in _DATE_DATA_TYPES:
        raise ValueError(
            "Dynamic date anchors require a verified date/time column."
        )

    return {
        DYNAMIC_DATE_ANCHOR_FIELD: DYNAMIC_DATE_ANCHOR,
        "table": table,
        "column": column,
    }


def validate_scalar_date_subquery(
    value: object,
    schema_context: dict,
    query_tables: set[str] | None = None,
    *,
    target_table: str,
    target_column: str,
) -> dict[str, object]:
    """Validate a typed scalar date subquery and its optional day offset."""
    if not isinstance(value, dict):
        raise ValueError("A scalar date subquery must be an object.")
    allowed_fields = {
        "scalar_subquery", "table", "column", "interval_days",
    }
    if (
        set(value) - allowed_fields
        or not {"scalar_subquery", "table", "column"}.issubset(value)
        or not isinstance(value.get("scalar_subquery"), str)
        or value["scalar_subquery"] not in SCALAR_DATE_SUBQUERY_OPERATIONS
    ):
        raise ValueError("Unsupported scalar date subquery.")

    table, column = value.get("table"), value.get("column")
    if (
        not isinstance(table, str)
        or not isinstance(column, str)
        or table not in schema_context
        or (query_tables is not None and table not in query_tables)
    ):
        raise ValueError("Scalar date subqueries must identify a table in the query.")

    def is_date_column(owner: str, name: str) -> bool:
        column_info = next(
            (
                item
                for item in schema_context.get(owner, [])
                if isinstance(item, dict) and item.get("column") == name
            ),
            None,
        )
        data_type = (
            column_info.get("data_type", "").casefold()
            if isinstance(column_info, dict)
            and isinstance(column_info.get("data_type"), str)
            else ""
        )
        return data_type in _DATE_DATA_TYPES

    if not is_date_column(table, column):
        raise ValueError(
            "Scalar date subqueries require a verified date/time source column."
        )
    if not is_date_column(target_table, target_column):
        raise ValueError(
            "Scalar date subqueries may only filter verified date/time columns."
        )

    normalized: dict[str, object] = {
        "scalar_subquery": value["scalar_subquery"],
        "table": table,
        "column": column,
    }
    if "interval_days" in value:
        interval_days = value["interval_days"]
        if (
            type(interval_days) is not int
            or not 1 <= interval_days <= 31
        ):
            raise ValueError("interval_days must be an integer from 1 to 31.")
        normalized["interval_days"] = interval_days
    return normalized


def validate_current_month_boundary(
    value: object,
    schema_context: dict,
    *,
    target_table: str,
    target_column: str,
) -> dict[str, str]:
    """Validate a bounded, database-clock month boundary token."""
    if (
        not isinstance(value, dict)
        or set(value) != {"calendar_boundary"}
        or not isinstance(value.get("calendar_boundary"), str)
        or value["calendar_boundary"] not in CURRENT_MONTH_BOUNDARIES
    ):
        raise ValueError("Unsupported current-month calendar boundary.")

    def is_date_column(owner: str, name: str) -> bool:
        column_info = next(
            (
                item
                for item in schema_context.get(owner, [])
                if isinstance(item, dict) and item.get("column") == name
            ),
            None,
        )
        data_type = (
            column_info.get("data_type", "").casefold()
            if isinstance(column_info, dict)
            and isinstance(column_info.get("data_type"), str)
            else ""
        )
        return data_type in _DATE_DATA_TYPES

    if not is_date_column(target_table, target_column):
        raise ValueError(
            "Current-month boundaries may only filter verified date/time columns."
        )
    return {"calendar_boundary": value["calendar_boundary"]}

PLAN_FEATURE_ATTRIBUTES = {
    "time_function_filter": (
        "function",
        "table",
        "column",
        "operator",
        "value",
    ),
    "scalar_date_subquery": (
        "scalar_subquery",
        "table",
        "column",
        "interval_days",
    ),
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
