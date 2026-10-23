"""
state.py — Shared state schema for the LangGraph pipeline.

Every node reads from and writes to this single state object as it moves
through the 11-node graph. Defined as a TypedDict (LangGraph's expected
shape for StateGraph) so each node's input/output type matches exactly
what generate_query.py, safety_check.py, etc. actually return today —
this file doesn't invent new shapes, it just wraps them.

Field naming follows each node's real return dict 1:1 wherever possible,
so graph.py can do state["field"] = node_output["field"] without any
translation layer.
"""

from typing import Any, Dict, List, Literal, NotRequired, Optional, TypedDict


# ---------------------------------------------------------------------------
# Per-node output shapes (mirrors what each node function actually returns)
# ---------------------------------------------------------------------------

class ClassifierResult(TypedDict):
    """Output of Node 1 — classify_and_rewrite()."""
    original_question: str
    rewritten_question: str
    complexity: Literal["simple", "complex"]
    reasoning: str
    intent: Literal["data", "chat", "clarification"]
    chat_reply: Optional[str]
    clarification_question: str
    clarification_options: List[str]
    clarification_option_records: NotRequired[List[Dict[str, Any]]]
    clarification_kind: NotRequired[str]
    clarification_topic: NotRequired[str]
    confirmation_complete: NotRequired[bool]
    catalog_filters: NotRequired[List[Dict[str, Any]]]
    catalog_filter_combiner: NotRequired[Literal["AND", "OR"]]
    unresolved_catalog_items: NotRequired[List[str]]
    cache_question: NotRequired[str]
    temporal_intents: NotRequired[List[Dict[str, Any]]]
    resolved_temporal_constraints: NotRequired[List[Dict[str, Any]]]
    current_state_filters: NotRequired[List[Dict[str, Any]]]
    shop_type_entities: NotRequired[List[Dict[str, Any]]]
    matched_business_rule_terms: NotRequired[List[str]]
    structured_catalog_validated: NotRequired[bool]


class SchemaColumn(TypedDict):
    """One column entry as stored in the schema cache (Node 3)."""
    column: str
    data_type: str
    nullable: bool
    key: str


# {table_name: [SchemaColumn, ...]}
SchemaContext = Dict[str, List[SchemaColumn]]


class GradeSampleResult(TypedDict):
    """Output of Node 4 — grade_schema_sufficiency()."""
    sufficient: bool
    schema: SchemaContext
    relationships: List[Dict[str, Any]]
    attempts: int


class BusinessRuleCondition(TypedDict, total=False):
    column: str
    operator: str
    value: Any


class BusinessRule(TypedDict, total=False):
    """One matched rule from business_rules.json (Node 5)."""
    term: str
    synonyms: List[str]
    query_logic: Dict[str, Any]  # contains "conditions": List[BusinessRuleCondition]


class SelectItem(TypedDict, total=False):
    table: str
    column: str
    date_bucket: Optional[Literal["month", "quarter", "year"]]
    condition: Dict[str, Any]
    period: Dict[str, str]
    period_comparison: Dict[str, Dict[str, str]]
    aggregate: Optional[str]
    alias: Optional[str]
    distinct: bool
    round_digits: int


class WhereCondition(TypedDict, total=False):
    table: str
    column: str
    operator: str
    value: Any


class JoinRelationship(TypedDict):
    table: str
    referenced_table: str
    constraint_name: str


class QueryJoin(TypedDict):
    table: str
    join_type: Literal["INNER", "LEFT"]
    relationship: JoinRelationship
    conditions: List[WhereCondition]


class OrderByItem(TypedDict, total=False):
    column: str
    metric: str
    table: str
    direction: str


class SearchOperation(TypedDict):
    term: str
    scope: Literal["shops"]


class GeneratedQuery(TypedDict, total=False):
    """Output of Node 6 — generate_query()."""
    table: str
    joins: List[QueryJoin]
    select_items: List[SelectItem]
    where_conditions: List[WhereCondition]
    combine_with: Literal["AND", "OR"]
    group_by: List[Dict[str, Any]]
    order_by: List[OrderByItem]
    distinct: bool
    search: Optional[SearchOperation]
    limit: int
    reasoning: str


class SafetyCheckResult(TypedDict):
    """Output of Node 7 — run_safety_check()."""
    sql: str
    params: List[Any]
    table: str
    row_limit: int


class ExecutionResult(TypedDict):
    """Output of Node 8 — run_query()."""
    results: List[Dict[str, Any]]
    columns: List[str]
    count: int
    row_count: int
    column_formats: Dict[str, Literal["identifier"]]
    suggestions: List["ValueSuggestion"]


class ValueSuggestion(TypedDict):
    """A database-backed alternative value that the user may choose."""
    column: str
    original: str
    suggested: str


class ResultGraderVerdict(TypedDict, total=False):
    """Output of Node 9 (not built yet) — shape reserved for it."""
    is_valid: bool
    flagged: bool
    flag_reason: Optional[str]


class EscalationInfo(TypedDict, total=False):
    """Populated when any retry loop (Node 4, 8, or 9) exhausts its cap."""
    node: Literal[
        "classifier_rewrite",
        "grade_sample",
        "generate_query",
        "safety_check",
        "run_self_fix",
        "result_grader",
    ]
    reason: str
    attempts_made: int


# ---------------------------------------------------------------------------
# Top-level graph state
# ---------------------------------------------------------------------------

class GraphState(TypedDict, total=False):
    """
    The single state object passed through every node in graph.py.
    `total=False` — not every field is populated at every point in the
    graph; a field only exists once the node that produces it has run.
    """

    # --- Input (set before the graph starts) ---
    user_question: str
    conversation_context: List[Dict[str, Any]]
    asked_items: List[str]
    selected_catalog_filters: List[Dict[str, Any]]
    cache_question: str
    widening_unclear_phrase: str | None
    widening_chosen_value: str | None

    # --- Node 1: Classifier + Rewrite ---
    classifier_result: ClassifierResult

    # --- Node 2: Cache Check ---
    cache_hit: bool
    cached_answer: Optional[Dict[str, Any]]  # whatever Node 10 originally saved

    # --- Node 3 / Node 4: Schema Grounding + Grade Sample ---
    grade_sample_result: GradeSampleResult
    schema_context: SchemaContext          # convenience alias of grade_sample_result["schema"]
    schema_relationships: List[Dict[str, Any]]
    grade_sample_attempts: int
    grade_sample_exhausted: bool

    # --- Node 5: Business Rules ---
    matched_business_rules: List[BusinessRule]

    # --- Node 6: Generate Query ---
    generated_query: GeneratedQuery
    answerable: bool
    unanswerable_reasoning: Optional[str]
    clarification_needed: bool
    clarification_question: str
    clarification_options: List[str]

    # --- Node 7: Safety Check ---
    safety_check_result: SafetyCheckResult
    safety_violation_reason: Optional[str]
    safety_repair_attempts: int

    # --- Node 8: Run + Self-Fix ---
    execution_result: ExecutionResult
    execution_attempts: int
    execution_exhausted: bool
    last_execution_error: Optional[str]

    # --- Node 9: Result Grader (async) ---
    result_grader_verdict: Optional[ResultGraderVerdict]
    result_grader_attempts: int
    result_grader_exhausted: bool

    # --- Node 10: Return + Save ---
    final_answer: Dict[str, Any]
    saved_to_cache: bool

    # --- Node 11: Human-in-the-Loop Escalation ---
    escalated: bool
    escalation_info: Optional[EscalationInfo]

    # --- Cross-cutting ---
    request_id: str          # for audit logging correlation across all node calls
    conversation_id: str
    context_version: str     # schema, relationship catalog, and business-rule fingerprint
    retry_caps: Dict[str, int]  # e.g. {"grade_sample": 3, "run_self_fix": 3, "result_grader": 3}
    diagnostics: Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_RETRY_CAPS: Dict[str, int] = {
    "grade_sample": 3,
    "run_self_fix": 3,
    "result_grader": 3,
}


def new_state(
    user_question: str,
    request_id: str,
    conversation_id: str | None = None,
    asked_items: List[str] | None = None,
    catalog_filters: List[Dict[str, Any]] | None = None,
    cache_question: str | None = None,
    widening_unclear_phrase: str | None = None,
    widening_chosen_value: str | None = None,
) -> GraphState:
    """
    Builds a fresh GraphState for a new incoming question. Call this at the
    top of the FastAPI /chat route before invoking the graph.
    """
    return GraphState(
        user_question=user_question,
        request_id=request_id,
        conversation_id=conversation_id or request_id,
        asked_items=asked_items or [],
        selected_catalog_filters=catalog_filters or [],
        cache_question=cache_question or user_question,
        widening_unclear_phrase=widening_unclear_phrase,
        widening_chosen_value=widening_chosen_value,
        conversation_context=[],
        retry_caps=dict(DEFAULT_RETRY_CAPS),
        diagnostics={
            "schema_attempts": [],
            "matched_business_rules": [],
            "query_plan": None,
            "generated_sql": None,
            "validation_attempts": [],
            "safety_results": [],
            "refusal": None,
            "infrastructure_errors": [],
        },
        cache_hit=False,
        escalated=False,
        saved_to_cache=False,
    )