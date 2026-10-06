"""
generate_query.py  (Node 6: Generate the Query — SQL version)

The core "brain" node. Takes the rewritten question plus grounding from
schema_grounding.py (real table/column structure) and business_rules.py
(matched rule definitions), and produces a structured description of a SQL
query — never raw SQL text, only JSON that safety_check.py turns into an
actual parameterized query.

Uses the STRONG model (per the project's cost rule).

Includes one bounded retry on validation failure. This is NOT a fix for a
bug — it's a safety net for a known, confirmed LLM behavior: even with a
well-tuned prompt, the model occasionally (not consistently) produces an
invalid or wrong-shaped response for the same question it usually gets
right (e.g. picking a plain column instead of COUNT(*) for "how many X").
Node 9 (result_grader) catches a wrong-but-valid-shaped answer downstream
regardless, but retrying here first is a better user experience: a 1-2
second delay and a correct answer, instead of every rare miss dead-ending
in an escalation.
"""

import logging
import math
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from app.agent.nodes.schema_grounding import get_ambiguous_names

from app.agent.prompts.generate_query_prompt import build_generate_query_messages
from app.services.llm_service import LLMServiceError, get_llm_service

logger = logging.getLogger(__name__)

ALLOWED_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE", "IS NULL", "IS NOT NULL", "BETWEEN"}
ALLOWED_AGGREGATES = {None, "COUNT", "SUM", "AVG", "MIN", "MAX"}
ALLOWED_DIRECTIONS = {"ASC", "DESC"}
ALLOWED_JOIN_TYPES = {"INNER", "LEFT"}
ALLOWED_BOOLEAN_OPERATORS = {"AND", "OR"}
MAX_QUERY_TABLES = 5
MAX_FILTER_DEPTH = 8
MAX_FILTER_TERMS = 64
MAX_USER_LIMIT = 1000
_VALID_ALIAS = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_RELATIVE_DATE_PATTERN = re.compile(
    r"\b(?:today|yesterday|tomorrow|expired|expires|right\s+now|as\s+of\s+now)\b"
    r"|\bat\s+the\s+moment\b"
    r"|\bcurrently\s+(?:valid|available)\b"
    r"|\b(?:this|last|previous)\s+(?:day|week|month|quarter|year)\b"
    r"|\b(?:last|past|previous)\s+\d+\s+days?\b",
    re.IGNORECASE,
)
_ACTIVE_OFFER_PATTERN = re.compile(
    r"\bactive\s+offers?\b"
    r"|\boffers?\b.{0,40}\bcurrently\s+active\b"
    r"|\bcurrently\s+active\s+offers?\b",
    re.IGNORECASE,
)
_SHOP_ACTIVE_STATUS_PATTERN = re.compile(
    r"\bshops?\b.{0,100}\bcurrently\s+active\b"
    r"|\bcurrently\s+active\b.{0,100}\bshops?\b",
    re.IGNORECASE,
)
_VERIFIED_ADDRESS_PATTERN = re.compile(
    r"\bverified\b.{0,40}\baddress(?:es)?\b"
    r"|\baddress(?:es)?\b.{0,40}\bverified\b",
    re.IGNORECASE,
)
_UNSUPPORTED_DISCOUNT_ARITHMETIC_PATTERN = re.compile(
    r"\b(?:total\s+discount(?:\s+value|\s+amount)?"
    r"|discount\s+(?:value|amount)"
    r"|discounts?\s+(?:of\s+)?(?:more|greater|above|over|at\s+least)"
    r"|savings)\b",
    re.IGNORECASE,
)
_USER_ACTIVE_SUBSCRIPTION_PATTERN = re.compile(
    r"\busers?\b.{0,80}\bactive\s+subscriptions?\b"
    r"|\bactive\s+subscriptions?\b.{0,80}\busers?\b",
    re.IGNORECASE,
)
_PLAN_ASSIGNMENT_PATTERN = re.compile(
    r"\bplan\b.{0,24}\bassigned\b|\bassigned\b.{0,24}\bplan\b",
    re.IGNORECASE,
)
_INTEGER_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year",
}
_DECIMAL_TYPES = {"decimal", "numeric"}
_FLOAT_TYPES = {"float", "double", "real"}
_COUNT_INTENT_PATTERN = re.compile(
    r"\b(?:how many|number of|count|counting|total number of)\b",
    re.IGNORECASE,
)
_EXPLICIT_EMPTY_GROUP_INTENT_PATTERN = re.compile(
    r"\b(?:each|every|all)\s+(?:(?:of\s+)?(?:the\s+)?)?[a-z][a-z_-]*\b",
    re.IGNORECASE,
)

MAX_ATTEMPTS = 2  # 1 real attempt + 1 retry on validation failure


class QueryGenerationError(Exception):
    """Raised when the LLM's response is malformed or fails validation, even after retrying."""

    def __init__(
        self,
        message: str = "",
        *,
        rule_id: str | None = None,
        stage: str = "generator",
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.stage = stage


class UnanswerableQuestionError(Exception):
    """
    Raised when the LLM determined the question can't be answered with the
    available schema and verified relationships. This is an honest
    "no", not a system bug — route it to the user as a clear message.
    Never retried — an honest "unanswerable" verdict is trusted immediately.

    `reasoning` is the user-facing text. `internal_reasoning` is the model's
    raw explanation: for logs only, never shown to users.
    """

    def __init__(self, reasoning: str, *, internal_reasoning: str | None = None) -> None:
        self.reasoning = reasoning
        self.internal_reasoning = internal_reasoning
        super().__init__(reasoning)


def preflight_refusals(
    rewritten_question: str,
    original_question: str | None = None,
) -> None:
    """Refuse questions with known unsupported temporal or derived-value semantics."""
    questions = (rewritten_question, original_question or "")

    # 1. Shop status / verified address (case 22). This runs first because
    #    "currently active" also matches the relative-date pattern, and
    #    shop questions need their own, more accurate message.
    if any(
        _SHOP_ACTIVE_STATUS_PATTERN.search(question)
        or _VERIFIED_ADDRESS_PATTERN.search(question)
        for question in questions
    ):
        raise UnanswerableQuestionError(
            "Shop active status and verified-address meaning are not defined "
            "in the available schema and business rules."
        )

    # 2. Relative dates and discount arithmetic (cases 7 and 19).
    has_relative_time = any(
        _RELATIVE_DATE_PATTERN.search(question) for question in questions
    )
    has_unsupported_discount = any(
        _UNSUPPORTED_DISCOUNT_ARITHMETIC_PATTERN.search(question)
        for question in questions
    )
    if has_relative_time and has_unsupported_discount:
        raise UnanswerableQuestionError(
            "I can't answer this reliably: the requested current-time window has "
            "no configured date policy, and discount value requires subtracting "
            "offer.offer_price from offer.actual_price, which the query plan "
            "cannot express."
        )
    if has_relative_time:
        raise UnanswerableQuestionError(
            "Relative dates cannot be interpreted reliably until a timezone and "
            "calendar policy are configured. Please use explicit dates."
        )
    if has_unsupported_discount:
        raise UnanswerableQuestionError(
            "I can't answer this reliably: discount value requires subtracting "
            "offer.offer_price from offer.actual_price, which the query plan "
            "cannot express safely."
        )

    # 3. "active offer" with no date wording at all. Still refused, with the
    #    clarifying message.
    if any(_ACTIVE_OFFER_PATTERN.search(question) for question in questions):
        raise UnanswerableQuestionError(
            "The meaning of 'active offer' is not configured. Please clarify "
            "whether active means a particular status value or a date condition."
        )

def _normalize_filter_expression(
    expression: Any,
    schema_context: Dict[str, Any],
    query_tables: set[str],
    *,
    aggregate_filters: bool = False,
) -> Dict[str, Any] | None:
    """Validate nested boolean predicates and resolve every column to a table."""
    if expression is None:
        return None

    term_count = 0

    def visit(node: Any, depth: int) -> Dict[str, Any]:
        nonlocal term_count
        if depth > MAX_FILTER_DEPTH:
            raise QueryGenerationError(
                f"Filter expression exceeds maximum depth {MAX_FILTER_DEPTH}."
            )
        if not isinstance(node, dict):
            raise QueryGenerationError("Each filter expression must be a JSON object.")

        boolean = node.get("boolean")
        if boolean is not None:
            conditions = node.get("conditions")
            if (
                boolean not in ALLOWED_BOOLEAN_OPERATORS
                or not isinstance(conditions, list)
                or not conditions
            ):
                raise QueryGenerationError(
                    "Boolean filter groups need AND/OR and at least one condition."
                )
            if len(conditions) == 1:
                return visit(conditions[0], depth + 1)
            return {
                "boolean": boolean,
                "conditions": [visit(condition, depth + 1) for condition in conditions],
            }

        term_count += 1
        if term_count > MAX_FILTER_TERMS:
            raise QueryGenerationError(
                f"Filter expression exceeds maximum of {MAX_FILTER_TERMS} conditions."
            )
        owner, column = _resolve_column(
            node,
            schema_context,
            query_tables,
            allow_star=aggregate_filters and node.get("column") == "*",
        )
        operator = node.get("operator")
        if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
            raise QueryGenerationError(f"Disallowed filter operator {operator!r}.")

        normalized: Dict[str, Any] = {
            "table": owner,
            "column": column,
            "operator": operator,
        }
        if aggregate_filters:
            aggregate = node.get("aggregate")
            if aggregate not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
                raise QueryGenerationError(
                    f"Disallowed HAVING aggregate {aggregate!r}."
                )
            normalized["aggregate"] = aggregate
        if operator not in {"IS NULL", "IS NOT NULL"}:
            value = node.get("value")
            if operator in {"IN", "NOT IN", "BETWEEN"}:
                expected_length = 2 if operator == "BETWEEN" else None
                if not isinstance(value, list) or not value or (
                    expected_length is not None and len(value) != expected_length
                ):
                    raise QueryGenerationError(
                        f"{operator} requires a "
                        f"{'[low, high]' if expected_length else 'non-empty list'} value."
                    )
            elif isinstance(value, (dict, list)):
                raise QueryGenerationError(
                    f"{operator} requires a scalar value."
                )
            data_type = _column_data_type(schema_context, owner, column)
            if operator in {"IN", "NOT IN", "BETWEEN"}:
                value = [
                    _coerce_filter_value(
                        item,
                        data_type,
                        table=owner,
                        column=column,
                    )
                    for item in value
                ]
            else:
                value = _coerce_filter_value(
                    value,
                    data_type,
                    table=owner,
                    column=column,
                )
            normalized["value"] = value
        return normalized

    return visit(expression, 0)


def _coerce_filter_value(
    value: Any,
    data_type: str | None,
    *,
    table: str,
    column: str,
) -> Any:
    """Convert numeric filter literals to the type declared by the schema."""
    if not isinstance(data_type, str):
        return value
    normalized_type = data_type.lower()
    if normalized_type not in _INTEGER_TYPES | _DECIMAL_TYPES | _FLOAT_TYPES:
        return value
    if isinstance(value, bool):
        raise QueryGenerationError(
            f"Boolean value is invalid for numeric column {table}.{column}."
        )
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        if normalized_type in _INTEGER_TYPES:
            if number != number.to_integral_value():
                raise InvalidOperation
            return int(number)
        if normalized_type in _DECIMAL_TYPES:
            return number
        converted = float(number)
        if not math.isfinite(converted):
            raise InvalidOperation
        return converted
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise QueryGenerationError(
            f"Filter value {value!r} is not valid for numeric column "
            f"{table}.{column} ({data_type})."
        ) from exc


def _column_data_type(
    schema_context: Dict[str, Any], table: str, column: str
) -> str | None:
    for entry in schema_context.get(table, []):
        if isinstance(entry, dict) and entry.get("column") == column:
            data_type = entry.get("data_type")
            return data_type if isinstance(data_type, str) else None
    return None


def _expand_plain_wildcard_select_items(
    select_items: List[Dict[str, Any]],
    query_tables: set[str],
    schema_context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Expand row-list wildcards before the safety layer builds SQL."""
    expanded: List[Dict[str, Any]] = []
    used_aliases: set[str] = set()
    sensitive_columns = {
        "password",
        "remember_token",
        "fcm_token",
        "firebase_uid",
        "facebook_id",
    }

    for item in select_items:
        if not isinstance(item, dict):
            raise QueryGenerationError("Each select_item must be a JSON object.")
        if (
            item.get("column") != "*"
            or item.get("aggregate") is not None
        ):
            alias = item.get("alias")
            if isinstance(alias, str):
                used_aliases.add(alias)
            expanded.append(item)
            continue

        if len(query_tables) != 1:
            raise QueryGenerationError(
                "Plain wildcard row selections are only supported for a single table."
            )
        table = next(iter(query_tables))
        columns = [
            entry.get("column")
            for entry in schema_context.get(table, [])
            if isinstance(entry, dict)
            and isinstance(entry.get("column"), str)
            and entry["column"].lower() not in sensitive_columns
        ]
        if not columns:
            raise QueryGenerationError(
                f"No safe selectable columns are available for table '{table}'."
            )
        for column in columns:
            alias = column
            if alias in used_aliases:
                alias = f"{table}_{column}"
            used_aliases.add(alias)
            expanded.append({
                "table": table,
                "column": column,
                "aggregate": None,
                "distinct": False,
                "alias": alias,
            })

    return expanded


def _expression_leaves(expression: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    if expression is None:
        return []
    if "boolean" in expression:
        return [
            leaf
            for condition in expression["conditions"]
            for leaf in _expression_leaves(condition)
        ]
    return [expression]


def _table_name_forms(table: str) -> set[str]:
    forms = {table.casefold()}
    if table.casefold().endswith("ies"):
        forms.add(f"{table[:-3]}y".casefold())
    elif table.casefold().endswith("s") and not table.casefold().endswith("ss"):
        forms.add(table[:-1].casefold())
    else:
        forms.add(f"{table}s".casefold())
    return forms


def _enforce_at_least_one_joined_entity(
    question: str | None,
    where_expression: Dict[str, Any] | None,
    joins: List[Dict[str, Any]],
    schema_context: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not question:
        return where_expression

    match = re.search(
        r"\bat\s+least\s+one\s+([a-z][a-z0-9_]*)\b",
        question,
        re.IGNORECASE,
    )
    if not match:
        return where_expression

    entity = match.group(1).casefold()
    candidates = [
        join
        for join in joins
        if entity in _table_name_forms(join["table"])
    ]
    if len(candidates) != 1:
        raise QueryGenerationError(
            "An 'at least one' condition must identify exactly one joined entity table."
        )

    child_join = candidates[0]
    if child_join["join_type"] == "INNER":
        return where_expression

    child_table = child_join["table"]
    primary_keys = [
        column["column"]
        for column in schema_context.get(child_table, [])
        if isinstance(column, dict) and column.get("key") == "PRI"
    ]
    if len(primary_keys) != 1:
        raise QueryGenerationError(
            f"An 'at least one {entity}' condition requires a single-column "
            f"primary key on '{child_table}'."
        )

    existence_predicate = {
        "table": child_table,
        "column": primary_keys[0],
        "operator": "IS NOT NULL",
    }
    if where_expression is None:
        return existence_predicate
    if (
        where_expression.get("table") == child_table
        and where_expression.get("column") == primary_keys[0]
        and where_expression.get("operator") == "IS NOT NULL"
    ):
        return where_expression
    if where_expression.get("boolean") == "AND":
        conditions = where_expression["conditions"]
        if existence_predicate in conditions:
            return where_expression
        return {
            **where_expression,
            "conditions": [*conditions, existence_predicate],
        }
    return {
        "boolean": "AND",
        "conditions": [where_expression, existence_predicate],
    }


def _requires_user_active_subscription(question: str) -> bool:
    return bool(_USER_ACTIVE_SUBSCRIPTION_PATTERN.search(question))


def _has_users_subscriptions_relationship(
    relationships: List[Dict[str, Any]] | None,
) -> bool:
    return any(
        {relationship.get("table"), relationship.get("referenced_table")}
        == {"users", "subscriptions"}
        for relationship in relationships or []
        if isinstance(relationship, dict)
    )


def _validate_user_active_subscription_plan(
    query: Dict[str, Any],
    question: str,
    schema_context: Dict[str, Any],
) -> None:
    """Prevent user/subscription questions from silently using a proxy field."""
    if not _requires_user_active_subscription(question):
        return
    query_tables = {query.get("table")}
    query_tables.update(
        join.get("table")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    )
    if not {"users", "subscriptions"}.issubset(query_tables):
        raise QueryGenerationError(
            "Questions about users with active subscriptions must query both "
            "users and subscriptions; a user-side status field is not a substitute."
        )

    predicates = _expression_leaves(query.get("where_expression"))
    if not any(
        predicate.get("table") == "subscriptions"
        and predicate.get("column") == "status"
        and predicate.get("operator") == "="
        and isinstance(predicate.get("value"), str)
        and predicate["value"].casefold() == "active"
        for predicate in predicates
    ):
        raise QueryGenerationError(
            "Questions about active subscriptions must filter "
            "subscriptions.status = 'active'."
        )

    if _PLAN_ASSIGNMENT_PATTERN.search(question) and not any(
        predicate.get("table") == "users"
        and predicate.get("column") == "plan_id"
        and predicate.get("operator") == "IS NOT NULL"
        for predicate in predicates
    ):
        raise QueryGenerationError(
            "Questions requiring an assigned plan must filter "
            "users.plan_id IS NOT NULL."
        )

    if _COUNT_INTENT_PATTERN.search(question):
        user_primary_keys = [
            entry.get("column")
            for entry in schema_context.get("users", [])
            if isinstance(entry, dict) and entry.get("key") == "PRI"
        ]
        if len(user_primary_keys) != 1 or not any(
            item.get("table") == "users"
            and item.get("column") == user_primary_keys[0]
            and item.get("aggregate") == "COUNT"
            and item.get("distinct") is True
            for item in query.get("select_items", [])
            if isinstance(item, dict)
        ):
            raise QueryGenerationError(
                "A joined count of users with active subscriptions must count "
                "distinct users by their single-column primary key."
            )


def _build_highest_booking_value_query(
    questions: tuple[str, ...],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
) -> Dict[str, Any] | None:
    intent_pattern = re.compile(
        r"\b(?:highest|largest|greatest|maximum|max|most)\b"
        r".{0,100}\bbooking\s+value\b",
        re.I,
    )
    normalized_questions = (
        question.replace("_", " ")
        for question in questions
        if question
    )
    if not any(intent_pattern.search(question) for question in normalized_questions):
        return None
    relationship = next(
        (
            edge for edge in relationships or []
            if edge.get("constraint_name") == "reviewed_bookings_shop_id"
            and edge.get("table") == "bookings"
            and edge.get("referenced_table") == "shop_details"
            and edge.get("cardinality") == "many_to_one"
        ),
        None,
    )
    required_columns = {
        "bookings": {"id", "shop_id", "final_amount"},
        "shop_details": {"id", "name"},
    }
    if relationship is None or any(
        not columns.issubset({
            column.get("column")
            for column in schema_context.get(table, [])
            if isinstance(column, dict)
        })
        for table, columns in required_columns.items()
    ):
        return None

    return {
        "answerable": True,
        "table": "bookings",
        "joins": [{
            "table": "shop_details",
            "join_type": "INNER",
            "relationship": {
                "constraint_name": relationship["constraint_name"],
                "table": "bookings",
                "referenced_table": "shop_details",
            },
        }],
        "select_items": [
            {
                "table": "shop_details",
                "column": "name",
                "aggregate": None,
                "alias": "shop_name",
            },
            {
                "table": "bookings",
                "column": "final_amount",
                "aggregate": "SUM",
                "distinct": False,
                "alias": "total_booking_value",
            },
        ],
        "where_expression": None,
        "group_by": [
            {"table": "shop_details", "column": "id"},
            {"table": "shop_details", "column": "name"},
        ],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [{"column": "total_booking_value", "direction": "DESC"}],
        "limit": 1,
        "reasoning": (
            "Sum booking final amounts per shop, order by the total descending, "
            "and return the highest-valued shop."
        ),
    }


def _business_semantic_contracts(
    matched_business_rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [
        contract
        for rule in matched_business_rules
        if isinstance(rule, dict)
        for contract in [
            rule.get("query_logic", {}).get("semantic_contract")
            if isinstance(rule.get("query_logic"), dict)
            else None
        ]
        if isinstance(contract, dict)
    ]


def _select_business_semantic_contracts(
    question: str,
    matched_business_rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    contracts = _business_semantic_contracts(matched_business_rules)
    preferred_kind = (
        "count_entities_by_group_aggregate_threshold"
        if _COUNT_INTENT_PATTERN.search(question)
        else "list_entities_by_group_aggregate_threshold"
    )
    preferred = [
        contract for contract in contracts
        if contract.get("kind") == preferred_kind
    ]
    return preferred or contracts


def _threshold_from_question(
    question: str, contract: Dict[str, Any]
) -> float:
    try:
        match = re.search(contract["threshold_pattern"], question, re.IGNORECASE)
    except (KeyError, re.error) as exc:
        raise QueryGenerationError(
            "Business rule contains an invalid threshold pattern."
        ) from exc
    if match is None:
        raise UnanswerableQuestionError(
            "Please specify the numeric rating threshold so I can calculate the result."
        )
    try:
        threshold = float(match.group(1))
    except (IndexError, ValueError) as exc:
        raise QueryGenerationError(
            "Business rule threshold pattern did not capture a valid number."
        ) from exc
    if not math.isfinite(threshold):
        raise UnanswerableQuestionError("The requested threshold must be a finite number.")
    return threshold


def _build_business_contract_query(
    question: str,
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    threshold = _threshold_from_question(question, contract)
    source_table = contract["source_table"]
    entity_table = contract["entity_table"]
    entity_key = contract["entity_key"]
    relationship_name = contract["relationship"]
    measure_column = contract["measure_column"]
    aggregate = contract["aggregate"]
    operator = contract["operator"]

    if source_table not in schema_context or entity_table not in schema_context:
        raise UnanswerableQuestionError(
            "The required source or entity table is not present in the verified schema."
        )
    entity_columns = schema_context.get(entity_table, [])
    entity_key_info = next(
        (
            column
            for column in entity_columns
            if isinstance(column, dict) and column.get("column") == entity_key
        ),
        None,
    )
    if not isinstance(entity_key_info, dict) or entity_key_info.get("key") != "PRI":
        raise UnanswerableQuestionError(
            "The entity identifier required by this metric is not a verified primary key."
        )
    measure_info = next(
        (
            column
            for column in schema_context.get(source_table, [])
            if isinstance(column, dict) and column.get("column") == measure_column
        ),
        None,
    )
    measure_type = measure_info.get("data_type") if isinstance(measure_info, dict) else None
    if not isinstance(measure_type, str) or measure_type.lower() not in {
        "tinyint",
        "smallint",
        "mediumint",
        "int",
        "integer",
        "bigint",
        "decimal",
        "numeric",
        "float",
        "double",
        "real",
    }:
        raise UnanswerableQuestionError(
            "The measure required by this metric is not a numeric column in the verified schema."
        )
    is_list_contract = (
        contract["kind"] == "list_entities_by_group_aggregate_threshold"
    )
    if is_list_contract:
        label_column = contract["entity_label_column"]
        label_info = next(
            (
                column
                for column in entity_columns
                if isinstance(column, dict) and column.get("column") == label_column
            ),
            None,
        )
        review_key = contract["count_column"]
        review_key_info = next(
            (
                column
                for column in schema_context[source_table]
                if isinstance(column, dict) and column.get("column") == review_key
            ),
            None,
        )
        if not isinstance(label_info, dict):
            raise UnanswerableQuestionError(
                "The entity label required by this metric is not present in the verified schema."
            )
        if not isinstance(review_key_info, dict) or review_key_info.get("key") != "PRI":
            raise UnanswerableQuestionError(
                "The review identifier required by this metric is not a verified primary key."
            )

    relationship = next(
        (
            edge
            for edge in relationships or []
            if edge.get("constraint_name") == relationship_name
            and edge.get("table") == source_table
            and edge.get("referenced_table") == entity_table
        ),
        None,
    )
    if (
        relationship is None
        or relationship.get("cardinality") != "many_to_one"
        or relationship.get("verification")
        not in {"declared_foreign_key", "reviewed_manual_query"}
        or not any(
            pair.get("column") == contract.get("relationship_column")
            and pair.get("referenced_column") == entity_key
            for pair in relationship.get("column_pairs", [])
            if isinstance(pair, dict)
        )
    ):
        raise UnanswerableQuestionError(
            "The reviewed relationship needed to calculate this metric is unavailable."
        )

    query = {
        "answerable": True,
        "reasoning": (
            f"Count {entity_table} entities after grouping {source_table} rows by "
            f"{entity_key} and applying {aggregate}({measure_column}) {operator} {threshold}."
        ),
        "table": source_table,
        "joins": [{
            "table": entity_table,
            "join_type": "INNER",
            "relationship": {
                "constraint_name": relationship_name,
                "table": source_table,
                "referenced_table": entity_table,
            },
        }],
        "select_items": [{
            "table": source_table,
            "column": "*",
            "aggregate": "COUNT",
            "distinct": False,
            "alias": "qualifying_entity_count",
        }],
        "where_expression": None,
        "group_by": [{"table": entity_table, "column": entity_key}],
        "having_expression": {
            "aggregate": aggregate,
            "table": source_table,
            "column": measure_column,
            "operator": operator,
            "value": threshold,
        },
        "count_groups": True,
        "count_entity": None,
        "order_by": [],
    }
    if is_list_contract:
        query.update({
            "select_items": [
                {
                    "table": entity_table,
                    "column": entity_key,
                    "aggregate": None,
                    "alias": "shop_id",
                },
                {
                    "table": entity_table,
                    "column": label_column,
                    "aggregate": None,
                    "alias": "shop_name",
                },
                {
                    "table": source_table,
                    "column": measure_column,
                    "aggregate": aggregate,
                    "alias": contract["measure_alias"],
                },
                {
                    "table": source_table,
                    "column": review_key,
                    "aggregate": "COUNT",
                    "distinct": True,
                    "alias": contract["count_alias"],
                },
            ],
            "group_by": [
                {"table": entity_table, "column": entity_key},
                {"table": entity_table, "column": label_column},
            ],
            "count_groups": False,
            "count_entity": {"table": source_table, "column": review_key},
            "order_by": [{
                "column": contract["measure_alias"],
                "direction": "DESC",
            }],
        })
    return query


def _validate_semantic_contract(
    query: Dict[str, Any],
    contracts: List[Dict[str, Any]],
    question: str,
) -> None:
    for contract in contracts:
        expected_threshold = _threshold_from_question(question, contract)
        is_list_contract = (
            contract["kind"] == "list_entities_by_group_aggregate_threshold"
        )
        expected_group = {
            "table": contract["entity_table"],
            "column": contract["entity_key"],
        }
        expected_groups = [expected_group]
        expected_count_entity = None
        if is_list_contract:
            label_column = contract["entity_label_column"]
            expected_groups.append({
                "table": contract["entity_table"],
                "column": label_column,
            })
            expected_count_entity = {
                "table": contract["source_table"],
                "column": contract["count_column"],
            }
        having = _expression_leaves(query.get("having_expression"))
        if (
            query.get("table") != contract["source_table"]
            or query.get("count_groups") != (not is_list_contract)
            or len(query.get("joins", [])) != 1
            or query["joins"][0].get("table") != contract["entity_table"]
            or query["joins"][0].get("join_type") != "INNER"
            or query["joins"][0].get("relationship", {}).get("constraint_name")
            != contract["relationship"]
            or query.get("group_by") != expected_groups
            or len(having) != 1
            or query.get("count_entity") != expected_count_entity
        ):
            raise QueryGenerationError(
                "Generated query does not implement the required business metric."
            )
        actual = having[0]
        try:
            values_match = Decimal(str(actual.get("value"))) == Decimal(
                str(expected_threshold)
            )
        except InvalidOperation:
            values_match = False
        if (
            actual.get("aggregate") != contract["aggregate"]
            or actual.get("table") != contract["source_table"]
            or actual.get("column") != contract["measure_column"]
            or actual.get("operator") != contract["operator"]
            or not values_match
        ):
            raise QueryGenerationError(
                "Generated HAVING threshold does not match the business definition or question."
            )
        if is_list_contract:
            expected_select_items = [
                {
                    "table": contract["entity_table"],
                    "column": contract["entity_key"],
                    "aggregate": None,
                    "distinct": False,
                    "alias": "shop_id",
                },
                {
                    "table": contract["entity_table"],
                    "column": contract["entity_label_column"],
                    "aggregate": None,
                    "distinct": False,
                    "alias": "shop_name",
                },
                {
                    "table": contract["source_table"],
                    "column": contract["measure_column"],
                    "aggregate": contract["aggregate"],
                    "distinct": False,
                    "alias": contract["measure_alias"],
                },
                {
                    "table": contract["source_table"],
                    "column": contract["count_column"],
                    "aggregate": "COUNT",
                    "distinct": True,
                    "alias": contract["count_alias"],
                },
            ]
            if (
                query.get("select_items") != expected_select_items
                or query.get("order_by") != [{
                    "column": contract["measure_alias"],
                    "direction": "DESC",
                }]
            ):
                raise QueryGenerationError(
                    "Generated listing does not return the required entity, average, and review count."
                )


def _columns_for_table(schema_context: Dict[str, Any], table: str) -> set[str]:
    return {
        column["column"]
        for column in schema_context.get(table, [])
        if isinstance(column, dict) and isinstance(column.get("column"), str)
    }


def _resolve_column(
    item: Dict[str, Any],
    schema_context: Dict[str, Any],
    query_tables: set[str],
    *,
    allow_star: bool = False,
) -> tuple[str, str]:
    column = item.get("column")
    if not isinstance(column, str) or not column:
        raise QueryGenerationError(f"Column reference must be a non-empty string. Got: {column!r}")
    table = item.get("table")
    if column == "*" and allow_star:
        if table is not None and (not isinstance(table, str) or table not in query_tables):
            raise QueryGenerationError(f"Column '*' references unknown query table {table!r}.")
        return table or next(iter(query_tables)), column
    if table is not None:
        if (
            not isinstance(table, str)
            or table not in query_tables
            or column not in _columns_for_table(schema_context, table)
        ):
            raise QueryGenerationError(f"Unknown column reference {table}.{column}.")
        return table, column

    owners = [
        candidate for candidate in query_tables
        if column in _columns_for_table(schema_context, candidate)
    ]
    if len(owners) != 1:
        raise QueryGenerationError(
            f"Column {column!r} must identify its table; matching tables are {owners}."
        )
    return owners[0], column


def _normalize_grouped_dimension_count(
    parsed: Dict[str, Any],
    question: str | None,
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None,
) -> Dict[str, Any]:
    """Use verified dimension metadata when a grouped FK was selected for an "each" count."""
    if not question or not re.search(
        r"\b(each|per|by)\s+(?:the\s+)?categor(?:y|ies)(?:_id)?\b",
        question,
        re.I,
    ):
        return parsed
    if parsed.get("answerable") is not True or parsed.get("joins") not in (None, []):
        return parsed

    table = parsed.get("table")
    select_items = parsed.get("select_items")
    group_by = parsed.get("group_by")
    if (
        not isinstance(table, str)
        or not isinstance(select_items, list)
        or not isinstance(group_by, list)
        or len(group_by) != 1
    ):
        return parsed

    grouped = group_by[0]
    if not isinstance(grouped, dict) or grouped.get("table") != table:
        return parsed
    foreign_key = grouped.get("column")
    if not isinstance(foreign_key, str) or not foreign_key.endswith("_id"):
        return parsed

    plain_dimensions = [
        item for item in select_items
        if isinstance(item, dict)
        and item.get("aggregate") is None
        and item.get("table") == table
        and item.get("column") == foreign_key
    ]
    count_items = [
        item for item in select_items
        if isinstance(item, dict) and item.get("aggregate") == "COUNT"
    ]
    if len(plain_dimensions) != 1 or len(count_items) != 1:
        return parsed

    matching_edges = [
        edge for edge in relationships or []
        if edge.get("table") == table
        and any(
            isinstance(pair, dict) and pair.get("column") == foreign_key
            for pair in edge.get("column_pairs", [])
        )
    ]
    if len(matching_edges) != 1:
        return parsed

    relationship = matching_edges[0]
    parent = relationship.get("referenced_table")
    pairs = relationship.get("column_pairs", [])
    pair = next(
        (
            pair for pair in pairs
            if isinstance(pair, dict) and pair.get("column") == foreign_key
        ),
        None,
    )
    if not isinstance(parent, str) or not isinstance(pair, dict):
        return parsed

    parent_key = pair.get("referenced_column")
    parent_columns = schema_context.get(parent, [])
    if not isinstance(parent_key, str) or not any(
        isinstance(column, dict)
        and column.get("column") == parent_key
        and column.get("key") == "PRI"
        for column in parent_columns
    ):
        return parsed

    label_columns = [
        column["column"]
        for column in parent_columns
        if isinstance(column, dict)
        and isinstance(column.get("column"), str)
        and column["column"].lower().endswith("name")
    ]
    if len(label_columns) != 1:
        return parsed

    entity_keys = [
        column["column"]
        for column in schema_context.get(table, [])
        if isinstance(column, dict) and column.get("key") == "PRI"
    ]
    if len(entity_keys) != 1:
        return parsed

    result = dict(parsed)
    result["table"] = parent
    result["joins"] = [{
        "table": table,
        "join_type": "LEFT",
        "relationship": {
            "constraint_name": relationship.get("constraint_name"),
            "table": relationship.get("table"),
            "referenced_table": parent,
        },
    }]
    dimension_alias = plain_dimensions[0].get("alias")
    alias_prefix = (
        dimension_alias[:-3]
        if isinstance(dimension_alias, str) and dimension_alias.endswith("_id")
        else foreign_key[:-3]
    )
    result["select_items"] = [
        {
            "table": parent,
            "column": label_columns[0],
            "aggregate": None,
            "alias": f"{alias_prefix}_name",
        },
        {
            **count_items[0],
            "table": table,
            "column": entity_keys[0],
            "distinct": True,
            "alias": count_items[0].get("alias") or "count",
        },
    ]
    result["group_by"] = [
        {"table": parent, "column": parent_key},
        {"table": parent, "column": label_columns[0]},
    ]
    result["count_entity"] = {"table": table, "column": entity_keys[0]}
    if not result.get("order_by"):
        result["order_by"] = [{
            "column": result["select_items"][1]["alias"],
            "direction": "DESC",
        }]
    return result


def _and_terms(expression: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Predicates that must ALL hold: the expression itself, or the members of
    (nested) AND groups. Anything under an OR is excluded on purpose."""
    if expression is None:
        return []
    if "boolean" not in expression:
        return [expression]
    if expression["boolean"] != "AND":
        return []
    return [
        term
        for condition in expression["conditions"]
        for term in _and_terms(condition)
    ]

def _enforce_default_address_filter(
    where_expression: Dict[str, Any] | None,
    query_tables: set[str],
    schema_context: Dict[str, Any],
) -> None:
    """A shop can have several addresses; only the default one is its main
    address. Any query that uses shop_address (joined or alone, for listing,
    counting or grouping) must filter is_default = 1, so every question gives
    the same count no matter how the model words the plan."""
    if "shop_address" not in query_tables:
        return
    if "is_default" not in _columns_for_table(schema_context, "shop_address"):
        raise QueryGenerationError(
            "shop_address has no is_default column, so one address per shop cannot be guaranteed.",
            rule_id="address_default_column_missing",
        )
    if not any(
        term.get("table") == "shop_address"
        and term.get("column") == "is_default"
        and term.get("operator") == "="
        and str(term.get("value")) == "1"
        and not isinstance(term.get("value"), bool)
        for term in _and_terms(where_expression)
    ):
        raise QueryGenerationError(
            "Using shop_address requires the filter shop_address.is_default = 1 "
            "(only the default address counts as a shop's main address).",
            rule_id="address_requires_default",
        )


_NAME_FILTER_LABELS = {
    "subcategories.subcategoryname": "subcategory",
    "categories.categoriesname": "category",
}


def _reject_ambiguous_name_filters(
    where_expression: Dict[str, Any] | None,
) -> None:
    """A category/subcategory name used by several rows would silently merge
    unrelated groups, so ask the user to clarify instead."""
    ambiguous = get_ambiguous_names()
    if not ambiguous:
        return
    for leaf in _expression_leaves(where_expression):
        key = f"{leaf.get('table')}.{leaf.get('column')}"
        label = _NAME_FILTER_LABELS.get(key)
        repeated = ambiguous.get(key)
        if label is None or not repeated:
            continue
        if leaf.get("operator") not in {"=", "IN", "LIKE"}:
            continue
        value = leaf.get("value")
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip("% ").casefold() in repeated:
                raise UnanswerableQuestionError(
                    f"The {label} name '{item.strip('% ')}' is used by more than "
                    f"one {label}. Please say which one you mean "
                    f"(for example, by naming its parent category)."
                )

_DISCOUNT_PATTERN = re.compile(r"\bdiscount(?:s|ed)?\b", re.IGNORECASE)


def _validate_discount_source(
    query: Dict[str, Any],
    question_text: str,
    schema_context: Dict[str, Any],
) -> None:
    """A discount question must not be answered from another table's discount
    column unless the user named that table. The official discount is
    offer.actual_price - offer.offer_price (see business_rules.json)."""
    if not _DISCOUNT_PATTERN.search(question_text):
        return
    tables = {query.get("table")}
    tables.update(
        join.get("table")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    )
    if "offer" in tables:
        return
    discount_tables = [
        table
        for table in tables
        if isinstance(table, str)
        and any(
            "discount" in column.casefold()
            for column in _columns_for_table(schema_context, table)
        )
    ]
    if not discount_tables:
        return
    words = set(re.findall(r"[a-z0-9_]+", question_text.casefold()))
    if any(words & _table_name_forms(table) for table in discount_tables):
        return
    raise UnanswerableQuestionError(
        "I can't answer this reliably: the discount you asked about is not "
        "stored as a single value I can use safely. Please say which kind of "
        "discount you mean."
    )


_DISCOUNT_AMOUNT_PATTERN = re.compile(r"\bdiscounts?\b", re.IGNORECASE)
_OFFER_PRICE_COLUMNS = {"actual_price", "offer_price"}


def _iter_conditions(expression):
    """Yield every leaf condition from a nested where/having expression."""
    if not isinstance(expression, dict):
        return
    nested = expression.get("conditions")
    if isinstance(nested, list):
        for item in nested:
            yield from _iter_conditions(item)
    else:
        yield expression


def _validate_discount_not_price_substitute(
    query: Dict[str, Any],
    question_text: str,
) -> None:
    """'discount' as an amount means actual_price - offer_price, which the plan
    format cannot express. Filtering or aggregating on one price column alone
    is a different number, so refuse. 'discounted price' is exempt."""
    if not _DISCOUNT_AMOUNT_PATTERN.search(question_text):
        return
    checked = []
    for key in ("where_expression", "having_expression"):
        checked.extend(_iter_conditions(query.get(key)))
    checked.extend(
        item
        for item in query.get("select_items", [])
        if isinstance(item, dict) and item.get("aggregate")
    )
    for item in checked:
        if (
            item.get("table") == "offer"
            and item.get("column") in _OFFER_PRICE_COLUMNS
        ):
            raise UnanswerableQuestionError(
                "I can't answer this reliably: a discount amount needs the "
                "difference between the actual price and the offer price, "
                "which I can't calculate safely."
            )


def _reject_distant_anti_join(
    where_expression: Dict[str, Any] | None,
    base_table: str,
    normalized_joins: List[Dict[str, Any]],
) -> None:
    """'<table>.x IS NULL' on a LEFT-joined table only means 'no match' when that
    table joins directly to the base table. Through an intermediate table it
    returns entities with at least one non-matching row."""
    for term in _and_terms(where_expression):
        if term.get("operator") != "IS NULL":
            continue
        owner = term.get("table")
        join = next((j for j in normalized_joins if j["table"] == owner), None)
        if join is None or join["join_type"] != "LEFT":
            continue
        relationship = join["relationship"]
        parent = (
            relationship["referenced_table"]
            if relationship["table"] == owner
            else relationship["table"]
        )
        if parent != base_table:
            raise QueryGenerationError(
                "IS NULL on a table joined through an intermediate table does not "
                "mean 'has none'. Group by the entity and use HAVING COUNT(...) = 0.",
                rule_id="anti_join_not_adjacent",
            )


def _reject_unused_left_join(
    normalized_joins: List[Dict[str, Any]],
    select_items: List[Dict[str, Any]],
    where_expression: Dict[str, Any] | None,
    group_by: List[Dict[str, Any]],
    having_expression: Dict[str, Any] | None,
    order_by: List[Dict[str, Any]],
    count_entity: Dict[str, Any] | None,
) -> None:
    """A LEFT-joined table that no clause references changes nothing. It means
    a condition such as 'has no X' was dropped, so the answer would be wrong."""
    used = {item.get("table") for item in select_items}
    used.update(leaf.get("table") for leaf in _expression_leaves(where_expression))
    used.update(leaf.get("table") for leaf in _expression_leaves(having_expression))
    used.update(item.get("table") for item in group_by)
    used.update(item.get("table") for item in order_by)
    if count_entity:
        used.add(count_entity.get("table"))
       # Tables that another join hangs off are bridges, not unused joins.
    bridges = set()
    for join in normalized_joins:
        relationship = join["relationship"]
        other = (
            relationship["referenced_table"]
            if relationship["table"] == join["table"]
            else relationship["table"]
        )
        bridges.add(other)
    for join in normalized_joins:
        if (
            join["join_type"] == "LEFT"
            and join["table"] not in used
            and join["table"] not in bridges
        ):
            raise QueryGenerationError(
                f"The LEFT JOIN to '{join['table']}' is never used by any "
                "condition, so part of the question was dropped. For 'has no X', "
                "filter X's primary key IS NULL.",
                rule_id="unused_left_join",
            )
        
      
def _log_rejected_count_plan(
    rule_id: str,
    attempt_number: int | None,
    base_table: str,
    owner: str,
    column: str,
    normalized_joins: List[Dict[str, Any]],
) -> None:
    """Structure-only diagnostics for joined COUNT rejections. Never raises."""
    try:
        join_evidence = [
            {
                "joined_table": join["table"],
                "relationship_table": join["relationship"]["table"],
                "referenced_table": join["relationship"]["referenced_table"],
                "cardinality": join["relationship"].get("cardinality"),
            }
            for join in normalized_joins
        ]
        logger.warning(
            "Node 6 rejected joined COUNT plan: rule_id=%s attempt=%r "
            "base_table=%r count_owner_table=%r count_column=%r joins=%r",
            rule_id, attempt_number, base_table, owner, column, join_evidence,
        )
    except Exception:
        pass  # diagnostics must never change behaviour


def _validate_llm_output(
    parsed: Dict[str, Any],
    schema_context: Dict[str, Any],
    relationships: List[Dict[str, Any]] | None = None,
    question: str | None = None,
    matched_business_rules: List[Dict[str, Any]] | None = None,
    attempt_number: int | None = None,
) -> Dict[str, Any]:
    """Validate and normalize the model's structured query against live schema."""
    if not isinstance(parsed, dict):
        raise QueryGenerationError("LLM response must be a JSON object.")
    parsed = _normalize_grouped_dimension_count(
        parsed, question, schema_context, relationships
    )
    required_keys = {"answerable", "reasoning"}
    missing = required_keys - parsed.keys()
    if missing:
        raise QueryGenerationError(f"LLM response missing required keys: {missing}. Got: {parsed}")

    if not isinstance(parsed["answerable"], bool):
        raise QueryGenerationError(f"'answerable' must be a boolean, got: {parsed['answerable']!r}")

    if not parsed["answerable"]:
        return parsed

    table = parsed.get("table")
    if not isinstance(table, str) or not table or table not in schema_context:
        raise QueryGenerationError(
            f"LLM chose table '{table}' which is not in the known schema: {list(schema_context.keys())}. "
            f"Refusing to trust a table name we have no schema evidence for."
        )

    joins = parsed.get("joins", [])
    if not isinstance(joins, list):
        raise QueryGenerationError("'joins' must be a list.")
    if len(joins) > MAX_QUERY_TABLES - 1:
        raise QueryGenerationError(
            f"Queries may use at most {MAX_QUERY_TABLES} tables, including the base table."
        )

    relationship_map = {
        (
            edge.get("constraint_name"),
            edge.get("table"),
            edge.get("referenced_table"),
        ): edge
        for edge in relationships or []
    }
    query_tables = {table}
    count_groups = parsed.get("count_groups", False)
    if not isinstance(count_groups, bool):
        raise QueryGenerationError("'count_groups' must be a boolean.")
    normalized_joins = []
    for join in joins:
        if not isinstance(join, dict):
            raise QueryGenerationError("Each join must be a JSON object.")
        new_table = join.get("table")
        join_type = join.get("join_type")
        relationship = join.get("relationship")
        if not isinstance(new_table, str) or new_table not in schema_context or new_table in query_tables:
            raise QueryGenerationError(f"Join introduces invalid or repeated table {new_table!r}.")
        if not isinstance(join_type, str) or join_type not in ALLOWED_JOIN_TYPES:
            raise QueryGenerationError(f"Unsupported join type {join_type!r}.")
        if not isinstance(relationship, dict):
            raise QueryGenerationError("Each join must identify a verified relationship.")
        if not all(
            isinstance(relationship.get(key), str)
            for key in ("constraint_name", "table", "referenced_table")
        ):
            raise QueryGenerationError("Join relationship identifiers must be strings.")
        relation_key = (
            relationship.get("constraint_name"),
            relationship.get("table"),
            relationship.get("referenced_table"),
        )
        verified = relationship_map.get(relation_key)
        if verified is None:
            raise QueryGenerationError(
                f"Join relationship {relation_key!r} is not in the verified schema catalog."
            )
        endpoints = {verified["table"], verified["referenced_table"]}
        existing_endpoints = endpoints & query_tables
        if new_table not in endpoints or len(existing_endpoints) != 1:
            raise QueryGenerationError(
                f"Join to {new_table!r} must connect it to exactly one table already in the query."
            )
        query_tables.add(new_table)
        normalized_joins.append({
            "table": new_table,
            "join_type": join_type,
            "relationship": {
                "constraint_name": verified.get("constraint_name", ""),
                "table": verified["table"],
                "referenced_table": verified["referenced_table"],
                "cardinality": verified.get("cardinality", "many_to_one"),
                "verification": verified.get("verification", "reviewed_manual_query"),
            },
        })

    select_items = parsed.get("select_items")
    if not isinstance(select_items, list) or not select_items:
        raise QueryGenerationError(f"'select_items' must be a non-empty list. Got: {select_items!r}")
    select_items = _expand_plain_wildcard_select_items(
        select_items,
        query_tables,
        schema_context,
    )

    normalized_select_items = []
    aliases = set()
    for item in select_items:
        if not isinstance(item, dict):
            raise QueryGenerationError("Each select_item must be a JSON object.")
        owner, column = _resolve_column(item, schema_context, query_tables, allow_star=True)
        aggregate = item.get("aggregate")
        if aggregate is not None and (
            not isinstance(aggregate, str) or aggregate not in ALLOWED_AGGREGATES
        ):
            raise QueryGenerationError(
                f"select_items uses disallowed aggregate {aggregate!r}. Allowed: {ALLOWED_AGGREGATES}"
            )
        distinct = item.get("distinct", False)
        if not isinstance(distinct, bool) or (distinct and (aggregate != "COUNT" or column == "*")):
            raise QueryGenerationError("distinct is supported only for COUNT of a named column.")
        if (
            len(query_tables) > 1
            and aggregate == "COUNT"
            and not distinct
            and not count_groups
        ):
            _log_rejected_count_plan(
                "joined_count_not_distinct",
                attempt_number,
                table,
                owner,
                column,
                normalized_joins,
            )
            raise QueryGenerationError(
                "COUNT in a multi-table query must count DISTINCT entity keys to prevent join fan-out.",
                rule_id="joined_count_not_distinct",
            )
        if len(query_tables) > 1 and aggregate == "COUNT" and not count_groups:
            primary_keys = [
                column_info["column"]
                for column_info in schema_context.get(owner, [])
                if isinstance(column_info, dict) and column_info.get("key") == "PRI"
            ]
            if len(primary_keys) != 1 or column != primary_keys[0]:
                _log_rejected_count_plan(
                    "joined_count_not_primary_key",
                    attempt_number,
                    table,
                    owner,
                    column,
                    normalized_joins,
                )
                raise QueryGenerationError(
                    "Joined COUNT must use the single-column primary key of the entity being counted.",
                    rule_id="joined_count_not_primary_key",
                )
        if len(query_tables) > 1 and aggregate in {"SUM", "AVG", "MIN", "MAX"}:
            aggregate_on_base_table = owner == table
            all_joins_many_to_one = all(
                join["table"] == join["relationship"]["referenced_table"]
                and join["relationship"].get("cardinality") == "many_to_one"
                for join in normalized_joins
            )
            if not aggregate_on_base_table or not all_joins_many_to_one:
                join_evidence = [
                    {
                        "joined_table": join["table"],
                        "relationship_table": join["relationship"]["table"],
                        "referenced_table": join["relationship"]["referenced_table"],
                        "cardinality": join["relationship"].get("cardinality"),
                    }
                    for join in normalized_joins
                ]
                try:
                    logger.warning(
                        "Node 6 rejected joined numeric aggregate plan: "
                        "attempt=%r base_table=%r aggregate=%r "
                        "aggregate_owner_table=%r aggregate_column=%r "
                        "aggregate_on_base_table=%s "
                        "all_joins_many_to_one=%s joins=%r",
                        attempt_number,
                        table,
                        aggregate,
                        owner,
                        column,
                        aggregate_on_base_table,
                        all_joins_many_to_one,
                        join_evidence,
                    )
                except Exception as logging_error:
                    try:
                        os.write(
                            2,
                            (
                                "Node 6 aggregate diagnostic logging failed "
                                f"({type(logging_error).__name__}).\n"
                            ).encode("ascii", "replace"),
                        )
                    except OSError:
                        pass
                raise QueryGenerationError(
                    "Joined numeric aggregates must measure the base table and join only to its many-to-one parents.",
                    rule_id="fanout_select_aggregate",
                )
        alias = item.get("alias")
        if not isinstance(alias, str) or not _VALID_ALIAS.fullmatch(alias):
            raise QueryGenerationError(f"Each selected expression needs a safe snake_case alias; got {alias!r}.")
        if alias in aliases:
            raise QueryGenerationError(f"Duplicate selected alias {alias!r}.")
        aliases.add(alias)
        round_digits = item.get("round_digits")
        if round_digits is not None and (
            type(round_digits) is not int
            or not 0 <= round_digits <= 6
            or aggregate not in {"SUM", "AVG", "MIN", "MAX"}
        ):
            raise QueryGenerationError(
                "round_digits must be an integer from 0 to 6 on a numeric aggregate."
            )
        normalized_item = {
            "table": owner,
            "column": column,
            "aggregate": aggregate,
            "distinct": distinct,
            "alias": alias,
        }
        if round_digits is not None:
            normalized_item["round_digits"] = round_digits
        normalized_select_items.append(normalized_item)

    legacy_where = parsed.get("where_conditions")
    where_expression = parsed.get("where_expression")
    if legacy_where is not None and where_expression is not None:
        raise QueryGenerationError(
            "Use either where_expression or the legacy where_conditions, not both."
        )
    if where_expression is None and legacy_where is not None:
        if not isinstance(legacy_where, list):
            raise QueryGenerationError("'where_conditions' must be a list.")
        combine_with = parsed.get("combine_with", "AND")
        if combine_with not in ALLOWED_BOOLEAN_OPERATORS:
            raise QueryGenerationError(
                f"'combine_with' must be AND or OR, got {combine_with!r}."
            )
        if len(legacy_where) == 1:
            where_expression = legacy_where[0]
        elif legacy_where:
            where_expression = {
                "boolean": combine_with,
                "conditions": legacy_where,
            }
    normalized_where_expression = _normalize_filter_expression(
        where_expression, schema_context, query_tables
    )
    normalized_where_expression = _enforce_at_least_one_joined_entity(
        question,
        normalized_where_expression,
        normalized_joins,
        schema_context,
    )
    _enforce_default_address_filter(
        normalized_where_expression, query_tables, schema_context
    )
    _reject_ambiguous_name_filters(normalized_where_expression)
    _reject_distant_anti_join(normalized_where_expression, table, normalized_joins)

    group_by = parsed.get("group_by", [])
    if not isinstance(group_by, list):
        raise QueryGenerationError("'group_by' must be a list.")
    normalized_group_by = []
    for expression in group_by:
        if isinstance(expression, str):
            expression = {"table": table, "column": expression}
        if not isinstance(expression, dict):
            raise QueryGenerationError("Each group_by entry must identify a table and column.")
        owner, column = _resolve_column(expression, schema_context, query_tables)
        normalized_group_by.append({"table": owner, "column": column})

    if (
        question
        and re.search(
            r"\b(each|per)\s+(?:the\s+)?categor(?:y|ies)(?:_id)?\b",
            question,
            re.I,
        )
        and "categories" in query_tables
    ):
        category_keys = [
            entry["column"]
            for entry in schema_context.get("categories", [])
            if isinstance(entry, dict) and entry.get("key") == "PRI"
        ]
        if len(category_keys) == 1 and not any(
            group.get("table") == "categories"
            and group.get("column") == category_keys[0]
            for group in normalized_group_by
        ):
            normalized_group_by.append({
                "table": "categories",
                "column": category_keys[0],
            })

    normalized_having_expression = _normalize_filter_expression(
        parsed.get("having_expression"),
        schema_context,
        query_tables,
        aggregate_filters=True,
    )
    if normalized_having_expression is not None and not normalized_group_by:
        raise QueryGenerationError("HAVING requires a grouped query.")

    aggregate_items = [
        item for item in normalized_select_items if item["aggregate"] is not None
    ]
    if aggregate_items:
        grouped_columns = {
            (item["table"], item["column"]) for item in normalized_group_by
        }
        for item in normalized_select_items:
            if item["aggregate"] is None and (
                not normalized_group_by
                or (item["table"], item["column"]) not in grouped_columns
            ):
                raise QueryGenerationError(
                    "Every plain selected column in an aggregate query must appear in GROUP BY."
                )

    if count_groups:
        if (
            len(normalized_select_items) != 1
            or normalized_select_items[0]["aggregate"] != "COUNT"
            or normalized_select_items[0]["column"] != "*"
            or normalized_select_items[0]["distinct"]
            or not normalized_group_by
            or normalized_having_expression is None
        ):
            raise QueryGenerationError(
                "count_groups requires COUNT(*), GROUP BY, and a HAVING expression."
            )
        if normalized_where_expression and any(
            leaf.get("aggregate") for leaf in _expression_leaves(normalized_where_expression)
        ):
            raise QueryGenerationError("Aggregate predicates must be placed in HAVING.")

    count_entity = None
    joined_counts = [
        item for item in normalized_select_items
        if item["aggregate"] == "COUNT"
    ] if normalized_joins and not count_groups else []
    requested_count_entity = parsed.get("count_entity")
    if joined_counts:
        if not isinstance(requested_count_entity, dict):
            raise QueryGenerationError(
                "A joined COUNT must identify the entity whose primary key is being counted."
            )
        entity_table, entity_column = _resolve_column(
            requested_count_entity, schema_context, query_tables
        )
        entity_primary_keys = [
            column_info["column"]
            for column_info in schema_context.get(entity_table, [])
            if isinstance(column_info, dict) and column_info.get("key") == "PRI"
        ]
        if len(entity_primary_keys) != 1 or entity_column != entity_primary_keys[0]:
            raise QueryGenerationError(
                "count_entity must identify a table's single-column primary key."
            )
        if any(
            item["table"] != entity_table or item["column"] != entity_column
            for item in joined_counts
        ):
            raise QueryGenerationError(
                "Every joined COUNT must count the declared count_entity primary key."
            )
        count_entity = {"table": entity_table, "column": entity_column}
    # Treat a stray count_entity as null when the query has no joined COUNT.
    # LLMs sometimes copy this optional field into simple grouped queries; it
    # has no SQL effect and should not invalidate an otherwise safe query.

    order_by = parsed.get("order_by", [])
    if not isinstance(order_by, list):
        raise QueryGenerationError("'order_by' must be a list.")
    normalized_order_by = []
    for order in order_by:
        if not isinstance(order, dict):
            raise QueryGenerationError("Each order_by entry must be a JSON object.")
        column = order.get("column")
        if not isinstance(column, str) or not column:
            raise QueryGenerationError("order_by requires a column name or selected alias.")
        direction = order.get("direction")
        if not isinstance(direction, str) or direction not in ALLOWED_DIRECTIONS:
            raise QueryGenerationError(f"order_by uses invalid direction '{order.get('direction')}'.")
        if column in aliases:
            normalized_order_by.append({"column": column, "direction": direction})
        else:
            owner, column = _resolve_column(order, schema_context, query_tables)
            normalized_order_by.append({
                "table": owner,
                "column": column,
                "direction": direction,
            })

    _reject_unused_left_join(
        normalized_joins,
        normalized_select_items,
        normalized_where_expression,
        normalized_group_by,
        normalized_having_expression,
        normalized_order_by,
        count_entity,
    )
    normalized_query = {
        "table": table,
        "joins": normalized_joins,
        "select_items": normalized_select_items,
        "where_expression": normalized_where_expression,
        "group_by": normalized_group_by,
        "having_expression": normalized_having_expression,
        "count_groups": count_groups,
        "count_entity": count_entity,
        "order_by": normalized_order_by,
        "include_empty_groups": parsed.get("include_empty_groups") is True,
        "reasoning": parsed["reasoning"],
    }
    if (
        normalized_query["include_empty_groups"]
        and question is not None
        and not _EXPLICIT_EMPTY_GROUP_INTENT_PATTERN.search(question)
    ):
        raise QueryGenerationError(
            "include_empty_groups requires an explicit request for each, every, or all group members."
        )
    limit = parsed.get("limit")
    if limit is not None and (
        type(limit) is not int or not 1 <= limit <= MAX_USER_LIMIT
    ):
        raise QueryGenerationError(
            f"'limit' must be an integer from 1 to {MAX_USER_LIMIT}."
        )
    if limit is not None:
        normalized_query["limit"] = limit
    contracts = _select_business_semantic_contracts(
        question or "",
        matched_business_rules or [],
    )
    if contracts:
        _validate_semantic_contract(
            normalized_query,
            contracts,
            question or "",
        )
    return normalized_query

_RETRY_NUDGE = (
    "Your previous response was invalid or incomplete. Respond again with the "
    "FULL JSON object described in the system prompt. In particular: include "
    "'reasoning' as the first field, and if the question asks for a count, "
    "total, average, sum, minimum, or maximum, make sure you used the correct "
    "aggregate function ('COUNT', 'SUM', 'AVG', 'MIN', or 'MAX') in select_items "
    "instead of selecting a plain row/column. For a joined COUNT, also include "
    "'count_entity' with the counted table and its single-column primary key. "
    "For questions about users with active subscriptions, query both users "
    "and subscriptions, filter subscriptions.status = 'active', and apply "
    "any requested users.plan_id IS NOT NULL condition. "
    "If your query uses shop_address in any way (even alone, for counting or "
    "grouping), add the filter shop_address.is_default = 1 "
    "at the top level of where_expression. "
    "For a query with no joins, or with no COUNT aggregate over joins, set "
    "'count_entity' to null."
)

_RETRY_HINT_BY_RULE = {
    "joined_count_not_primary_key": (
        "To count unique entities (for example unique users), join that "
        "entity's own table through its verified relationship and list it in "
        "joins. Then count that table's single-column primary key with "
        "distinct true, and set count_entity to the same table and column. "
        "Do not count a foreign key column such as reviews.user_id. Every "
        "table whose column you use must appear in joins."
    ),
    "fanout_select_aggregate": (
        "For a SUM, AVG, MIN, or MAX over joined tables, make the table that "
        "contains the measured column the base table. Join only upward through "
        "verified many-to-one relationships to its parent tables. Do not start "
        "from the entity table and join down to the measured rows."
    ),
}


def _retry_nudge_for(error: Exception) -> str:
    """Generic retry message, plus a specific hint when the rejection rule is known."""
    hint = _RETRY_HINT_BY_RULE.get(getattr(error, "rule_id", None))
    return _RETRY_NUDGE if hint is None else f"{_RETRY_NUDGE} {hint}"


async def generate_query(
    rewritten_question: str,
    schema_context: Dict[str, Any],
    matched_business_rules: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]] | None = None,
    original_question: str | None = None,
) -> Dict[str, Any]:
    """
    Main entry point for Node 6.

    Returns a dict:
        {
            "table": str,
            "joins": [...],
            "select_items": [...],  # optional round_digits on numeric aggregates
            "where_expression": predicate/group | None,
            "group_by": [...],
            "having_expression": predicate/group | None,
            "count_entity": {"table": str, "column": str} | None,
            "order_by": [...],
            "limit": int,  # present only when explicitly requested
            "reasoning": str,
        }
    Ready for safety_check.py to independently re-validate and build the
    final parameterized SQL from.

    Raises:
        UnanswerableQuestionError if the LLM determined the question can't
            be answered from the available schema. Never retried.
        QueryGenerationError if the LLM call failed, or if the response
            still fails validation after MAX_ATTEMPTS tries.
    """
    questions = (rewritten_question, original_question or "")
    preflight_refusals(rewritten_question, original_question)

    if any(_requires_user_active_subscription(question) for question in questions):
        if not _has_users_subscriptions_relationship(relationships):
            raise UnanswerableQuestionError(
                "This question requires a verified relationship between users "
                "and subscriptions, which is not currently configured."
            )

    deterministic_plan = _build_highest_booking_value_query(
        (rewritten_question, original_question or ""),
        schema_context,
        relationships,
    )
    if deterministic_plan is not None:
        return _validate_llm_output(
            deterministic_plan,
            schema_context,
            relationships,
            rewritten_question,
            matched_business_rules,
        )

    contracts = _select_business_semantic_contracts(
        rewritten_question,
        matched_business_rules,
    )
    if len(contracts) > 1:
        raise QueryGenerationError(
            "Multiple incompatible business semantic contracts matched this question."
        )
    if contracts:
        parsed = _build_business_contract_query(
            rewritten_question,
            schema_context,
            relationships,
            contracts[0],
        )
        validated = _validate_llm_output(
            parsed,
            schema_context,
            relationships,
            rewritten_question,
            matched_business_rules,
        )
        return validated

    messages = build_generate_query_messages(
        rewritten_question,
        schema_context,
        matched_business_rules,
        relationships,
        original_question=original_question,
    )
    llm = get_llm_service()

    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            parsed = await llm.call_agent_json("QUERY_GENERATOR", messages, temperature=0.0)
        except LLMServiceError as e:
            # A transport/provider failure is not the "occasionally wrong
            # shape" case this retry exists for — fail immediately, the
            # underlying LLMService already retried internally per its own
            # MAX_RETRIES policy.
            raise QueryGenerationError(f"LLM call failed during query generation: {e}") from e

        try:
            validated = _validate_llm_output(
                parsed,
                schema_context,
                relationships,
                rewritten_question,
                matched_business_rules,
                attempt_number=attempt,
            )
            if parsed.get("answerable") is True:
                for question in questions:
                    _validate_user_active_subscription_plan(
                        validated, question, schema_context
                    )
                _validate_discount_source(
                    validated, " ".join(questions), schema_context
                )
                _validate_discount_not_price_substitute(
                    validated, " ".join(questions)
                )

        except QueryGenerationError as e:
            last_error = e
            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "Node 6 attempt %d/%d failed validation, retrying: %s",
                    attempt, MAX_ATTEMPTS, e,
                )
                messages = messages + [
                    {"role": "assistant", "content": str(parsed)},
                    {"role": "user", "content": _retry_nudge_for(e)},
                ]
                continue
            logger.error(
                "Node 6 failed validation after %d attempt(s): %s", MAX_ATTEMPTS, e
            )
            raise QueryGenerationError(
                f"Node 6 failed validation after {MAX_ATTEMPTS} attempt(s): {e}",
                rule_id=getattr(e, "rule_id", None),
            ) from e

        # Validation passed on this attempt.
        if not parsed["answerable"]:
            logger.info("Question marked unanswerable: %s", parsed["reasoning"])
            raise UnanswerableQuestionError(
                "I can't answer this reliably with the data and verified "
                "relationships currently available.",
                internal_reasoning=parsed["reasoning"],
            )

        result = validated

        if attempt > 1:
            logger.info("Node 6 succeeded on retry attempt %d", attempt)

        logger.info(
            "Generated query across %d table(s): %s",
            1 + len(result["joins"]), result["reasoning"],
        )
        return result

    # Unreachable in practice (the loop always returns or raises), but kept
    # as an explicit guard rather than letting the function fall through
    # silently if MAX_ATTEMPTS is ever set to 0 by mistake.
    raise QueryGenerationError(f"Node 6 exhausted all attempts: {last_error}")