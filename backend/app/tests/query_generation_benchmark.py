"""Opt-in benchmark for schema retrieval and generated SQL plans.

Run from ``backend`` with:
    python -m app.tests.query_generation_benchmark
    python -m app.tests.query_generation_benchmark --execute --cases 19,21,25

This contacts the configured LLM services and MySQL INFORMATION_SCHEMA. By
default it runs ``EXPLAIN`` only; pass ``--execute`` to also execute generated
SQL on the read-only replica and record answer diagnostics. Results are written
to a separate CSV so existing benchmark evidence is preserved.
"""

import argparse
import asyncio
import csv
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from app.agent.nodes.business_rules import find_relevant_rules
from app.agent.nodes.classifier_rewrite import classify_and_rewrite
from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    UnanswerableQuestionError,
    generate_query,
    preflight_refusals,
)
from app.agent.nodes.grade_sample import (
    GradeSampleError,
    MissingRelationshipError,
    grade_schema_sufficiency,
)
from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check
from app.agent.nodes.schema_grounding import (
    get_cached_schema,
    refresh_schema_cache,
)
from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
    fetch_all,
)
from app.agent.nodes.run_self_fix import run_query
from app.services.embedding_service import build_schema_index
from app.agent.nodes import grade_sample

logger = logging.getLogger(__name__)


def _case(
    number: int,
    category: str,
    question: str,
    tables: Sequence[str],
    *,
    joins: Sequence[Tuple[str, str] | Tuple[str, str, str]] = (),
    selects: Sequence[Tuple[str, str, str | None, bool]] = (),
    filters: Sequence[Tuple[str, str, str, Any]] = (),
    groups: Sequence[Tuple[str, str]] = (),
    having: Sequence[Tuple[str, str, str, str, Any]] = (),
    order_by: Sequence[Tuple[str, str]] = (),
    limit: int | None = None,
    round_digits: int | None = None,
    row_listing: bool = False,
    filter_boolean: str | None = None,
    expected_outcome: str = "sql",
    note: str = "",
    alternatives: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    return {
        "number": number,
        "category": category,
        "question": question,
        "expected_tables": list(tables),
        "expected_joins": [
            {
                "table": join[0],
                "constraint_name": join[1],
                "join_type": join[2] if len(join) == 3 else "INNER",
            }
            for join in joins
        ],
        "expected_selects": [
            {
                "table": table,
                "column": column,
                "aggregate": aggregate,
                "distinct": distinct,
            }
            for table, column, aggregate, distinct in selects
        ],
        "expected_filters": [
            {"table": table, "column": column, "operator": operator, "value": value}
            for table, column, operator, value in filters
        ],
        "expected_groups": [
            {"table": table, "column": column} for table, column in groups
        ],
        "expected_having": [
            {
                "table": table,
                "column": column,
                "aggregate": aggregate,
                "operator": operator,
                "value": value,
            }
            for table, column, aggregate, operator, value in having
        ],
        "expected_order_by": [
            {"column": column, "direction": direction}
            for column, direction in order_by
        ],
        "expected_limit": limit,
        "expected_round_digits": round_digits,
        "expected_row_listing": row_listing,
        "expected_filter_boolean": filter_boolean,
        "expected_outcome": expected_outcome,
        "reference_note": note,
        "alternatives": list(alternatives),
    }


CASES = [
    _case(1, "simple", "How many shops do we have in total?", ["shop_details"],
          selects=[("shop_details", "*", "COUNT", False)]),
    _case(2, "simple", "How many offers are currently active?", ["offer"],
          selects=[("offer", "*", "COUNT", False)],
          filters=[("offer", "end_date", ">=", "__CURRENT_DATE__")],
          expected_outcome="refusal",
          note="Safe refusal until a trusted current-date and timezone policy is configured."),
    _case(3, "simple", "What is the highest offer price listed?", ["offer"],
          selects=[("offer", "offer_price", "MAX", False)]),
    _case(4, "simple", "How many users are registered in total?", ["users"],
          selects=[("users", "*", "COUNT", False)]),
    _case(5, "simple", "How many subscriptions have a status of active?",
          ["subscriptions"],
          selects=[("subscriptions", "*", "COUNT", False)],
          filters=[("subscriptions", "status", "=", "active")]),
    _case(6, "simple", "What is the price of the Premium Plan?", ["plans"],
          selects=[("plans", "price", None, False)],
          filters=[("plans", "name", "=", "Premium Plan")],
          note="A named-plan lookup does not imply a MAX aggregate."),
     _case(7, "moderate", "How many shops are in the Food & Dining category?",
          ["shop_details"],
          selects=[("shop_details", "*", "COUNT", False)],
          filters=[("shop_details", "category_id", "=", 2)],
          note="Category id 2 is verified as Food & Dining; direct-id and name-join plans are both valid.",
          alternatives=[
              _case(
                  7, "moderate", "How many shops are in the Food & Dining category?",
                  ["shop_details", "categories"],
                  joins=[("categories", "reviewed_shop_details_category_id")],
                  selects=[("shop_details", "id", "COUNT", True)],
                  filters=[("categories", "categoriesname", "=", "Food & Dining")],
              )
          ]),
    _case(8, "moderate", "How many offers have expired?", ["offer"],
          selects=[("offer", "*", "COUNT", False)],
          filters=[("offer", "end_date", "<", "__CURRENT_DATE__")],
          expected_outcome="refusal",
          note="Safe refusal until a trusted current-date and timezone policy is configured."),
    _case(9, "moderate", "List the number of shops in each category.",
          ["categories", "shop_details"],
          joins=[("shop_details", "reviewed_shop_details_category_id", "LEFT")],
          selects=[
              ("categories", "categoriesname", None, False),
              ("shop_details", "id", "COUNT", True),
          ],
          groups=[("categories", "categoriesname"), ("categories", "id")]),
    _case(10, "moderate", "How many shops have never posted an offer?",
          ["shop_details", "offer"],
          joins=[("offer", "reviewed_offer_shop_id", "LEFT")],
          selects=[("shop_details", "id", "COUNT", True)],
          filters=[("offer", "shop_id", "IS NULL", None)],
          note="The reviewed offer-to-shop relationship supports a LEFT JOIN anti-match."),
    _case(11, "moderate", "How many shops have zero bookings?",
          ["shop_details", "bookings"],
          joins=[("bookings", "reviewed_bookings_shop_id", "LEFT")],
          selects=[("shop_details", "id", "COUNT", True)],
          filters=[("bookings", "shop_id", "IS NULL", None)]),
    _case(12, "moderate", "How many users have no plan assigned?", ["users"],
          selects=[("users", "*", "COUNT", False)],
          filters=[("users", "plan_id", "IS NULL", None)]),
    _case(13, "moderate", "List the top 5 shops by number of offers.",
          ["shop_details", "offer"],
          joins=[("offer", "reviewed_offer_shop_id", "LEFT")],
          selects=[
              ("shop_details", "name", None, False),
              ("shop_details", "phone", None, False),
              ("shop_details", "websitelink", None, False),
              ("offer", "id", "COUNT", True),
          ],
          groups=[
              ("shop_details", "id"),
              ("shop_details", "name"),
              ("shop_details", "phone"),
              ("shop_details", "websitelink"),
          ],
          order_by=[("offer_count", "DESC")],
          limit=5,
          note="The rewritten request asks for shop contact fields; count distinct offers per shop."),
    _case(14, "moderate", "What is the average final amount across all bookings?",
          ["bookings"],
          selects=[("bookings", "final_amount", "AVG", False)],
          note="No rounding precision was requested."),
    _case(15, "moderate", "How many bookings came from Nagercoil?", ["bookings"],
          selects=[("bookings", "*", "COUNT", False)],
          filters=[("bookings", "city", "=", "Nagercoil")]),
    _case(16, "complex", "For each category, how many shops have at least one offer?",
          ["categories", "shop_details", "offer"],
          joins=[
              ("shop_details", "reviewed_shop_details_category_id", "LEFT"),
              ("offer", "reviewed_offer_shop_id", "LEFT"),
          ],
          selects=[
              ("categories", "categoriesname", None, False),
              ("shop_details", "id", "COUNT", True),
          ],
          filters=[("offer", "id", "IS NOT NULL", None)],
          groups=[("categories", "categoriesname"), ("categories", "id")],
          note="The verified offer relationship supports counting shops with at least one offer."),
    _case(17, "complex", "What is the total value of active offers, grouped by category?",
          ["categories", "shop_details", "offer"],
          joins=[
              ("shop_details", "reviewed_shop_details_category_id"),
              ("offer", "reviewed_offer_shop_id"),
          ],
          selects=[
              ("categories", "categoriesname", None, False),
              ("offer", "actual_price", "SUM", False),
          ],
          filters=[("offer", "end_date", ">=", "__CURRENT_DATE__")],
          groups=[("categories", "id"), ("categories", "categoriesname")],
          expected_outcome="refusal",
          note="Offer/shop relationship is reviewed; active-offer meaning and calendar policy remain unresolved."),
    _case(18, "complex",
          "How many users have an active subscription and a plan assigned?",
          ["users", "subscriptions"],
          joins=[("subscriptions", "reviewed_subscriptions_customer_id")],
          selects=[("users", "id", "COUNT", True)],
          filters=[
              ("subscriptions", "status", "=", "active"),
              ("users", "plan_id", "IS NOT NULL", None),
          ],
          filter_boolean="AND",
          expected_outcome="refusal",
          note="A safe refusal is required because no users-to-subscriptions relationship is verified."),
    _case(19, "complex",
          "Which shop has the highest total booking value, and how much?",
          ["shop_details", "bookings"],
          joins=[("shop_details", "reviewed_bookings_shop_id")],
          selects=[
              ("shop_details", "name", None, False),
              ("bookings", "final_amount", "SUM", False),
          ],
          groups=[("shop_details", "id"), ("shop_details", "name")],
          order_by=[("total_booking_value", "DESC")],
          limit=1,
          note="Sum bookings.final_amount from the booking base table, then rank shops.",
          alternatives=[
              _case(
                  19, "complex",
                  "Which shop has the highest total booking value, and how much?",
                  ["shop_details", "bookings"],
                  joins=[("shop_details", "reviewed_bookings_shop_id")],
                  selects=[
                      ("shop_details", "name", None, False),
                      ("bookings", "final_amount", "SUM", False),
                  ],
                  groups=[("bookings", "shop_id"), ("shop_details", "name")],
                  order_by=[("total_booking_value", "DESC")],
                  limit=1,
              )
          ]),
    _case(20, "complex", "For each city, what is the average booking amount?",
          ["bookings"],
          selects=[
              ("bookings", "city", None, False),
              ("bookings", "final_amount", "AVG", False)],
          groups=[("bookings", "city")],
          note="No rounding precision was requested."),
    _case(21, "complex",
          "List shops in the Transportation Services category that have more than 2 offers.",
          ["shop_details", "categories", "offer"],
          joins=[
              ("categories", "reviewed_shop_details_category_id"),
              ("offer", "reviewed_offer_shop_id"),
          ],
          selects=[
              ("shop_details", "name", None, False),
              ("shop_details", "phone", None, False),
              ("shop_details", "websitelink", None, False),
          ],
          filters=[("categories", "categoriesname", "=", "Transportation Services")],
          groups=[
              ("shop_details", "id"),
              ("shop_details", "name"),
              ("shop_details", "phone"),
              ("shop_details", "websitelink"),
          ],
          having=[("offer", "id", "COUNT", ">", 2)],
          order_by=[("name", "ASC")],
          note="The joined HAVING count filters qualifying shops; it is not a selected joined COUNT."),
    _case(22, "edge", "Show me the offers for shop ID 9999.", ["offer"],
          filters=[("offer", "shop_id", "=", 9999)],
          row_listing=True,
          note="The supplied reference SQL counts offers, while the question asks to list them; the benchmark follows the question."),
    _case(23, "edge", 'How many shops are in the "Luxury Goods" category?',
          ["shop_details", "categories"],
          joins=[("categories", "reviewed_shop_details_category_id")],
          selects=[("shop_details", "id", "COUNT", True)],
          filters=[("categories", "categoriesname", "=", "Luxury Goods")]),
    _case(24, "edge", "List all bookings with a negative final amount.", ["bookings"],
          filters=[("bookings", "final_amount", "<", 0)],
          row_listing=True,
          note="The supplied reference SQL counts rows, while the question asks to list them; the benchmark follows the question."),
    _case(25, "edge", "What is the average subscription amount for cancelled subscriptions?",
          ["subscriptions"],
          selects=[("subscriptions", "amount", "AVG", False)],
          filters=[("subscriptions", "status", "=", "cancelled")],
          note="This benchmark checks the AVG query only; it does not execute it to observe SQL NULL."),
]


CSV_FIELDS = [
    "case_number",
    "category",
    "question",
    "rewritten_question",
    "expected_outcome",
    "total_ms",
    "setup_ms",
    "classification_ms",
    "schema_grade_ms",
    "retrieval_calls_ms",
    "generation_ms",
    "business_rules_ms",
    "sql_validation_ms",
    "schema_attempts",
    "generation_status",
    "retrieved_tables_by_attempt",
    "selected_schema_tables",
    "expected_tables",
    "raw_retrieval_table_precision",
    "raw_retrieval_table_recall",
    "raw_retrieval_table_f1",
    "grounded_schema_table_precision",
    "grounded_schema_table_recall",
    "grounded_schema_table_f1",
    "required_relationship_recall",
    "verified_relationships_available",
    "matched_business_rules",
    "generated_base_table",
    "generated_joins",
    "generated_select_items",
    "generated_filters",
    "generated_group_by",
    "generated_having",
    "generated_order_by",
    "generated_query_plan",
    "compiled_sql",
    "sql_parameters",
    "safety_validation",
    "explain_validation",
    "plan_match",
    "plan_check_failures",
    "executed_row_count",
    "executed_preview",
    "truncated",
    "no_data_reason",
    "execution_answer_type",
    "execution_message",
    "execution_check",
    "reference_note",
    "error",
]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


_SENSITIVE_PREVIEW_COLUMN = re.compile(
    r"(?:^|_)(?:id|name|email|phone|address|amount|price|value|revenue|"
    r"password|token|secret)(?:_|$)",
    re.IGNORECASE,
)


def _safe_execution_preview(
    results: Sequence[Dict[str, Any]], limit: int = 3
) -> List[Dict[str, Any]]:
    """Keep a small preview while redacting identifiers, PII, and financial values."""
    return [
        {
            column: "<redacted>" if _SENSITIVE_PREVIEW_COLUMN.search(column) else value
            for column, value in row.items()
        }
        for row in results[:limit]
    ]


def _execution_columns(query: Dict[str, Any], schema_context: Dict[str, Any]) -> List[str]:
    columns = []
    for item in query.get("select_items", []):
        column = item.get("column")
        aggregate = item.get("aggregate")
        alias = item.get("alias")
        if column == "*" and aggregate is None:
            columns.extend(
                schema_column["column"]
                for schema_column in schema_context.get(query.get("table"), [])
                if isinstance(schema_column, dict) and "column" in schema_column
            )
        elif alias:
            columns.append(alias)
        elif aggregate:
            columns.append(f"{aggregate.lower()}_{'all' if column == '*' else column}")
        else:
            columns.append(column)
    return columns


def _check_execution_expectation(
    case_number: int, execution: Dict[str, Any]
) -> str:
    results = execution.get("results", [])
    if case_number == 19:
        if len(results) != 1 or execution.get("truncated"):
            return "failed"
        result = results[0]
        try:
            matches_expected = (
                result.get("shop_name") == "Boss App Studio Pvt Ltd"
                and float(result.get("total_booking_value")) == 82191.0
            )
        except (TypeError, ValueError):
            return "failed"
        return "passed" if matches_expected else "failed"
    if case_number in {21, 23}:
        if case_number == 21:
            return "passed" if not results else "failed"
        if len(results) != 1 or len(results[0]) != 1:
            return "failed"
        return (
            "passed"
            if next(iter(results[0].values())) == 0
            else "failed"
        )
    if case_number == 25:
        return (
            "passed"
            if execution.get("no_data_reason") == "no_rows"
            else "failed"
        )
    return "not_applicable"


async def _explain_query(sql: str, params: Sequence[Any]) -> None:
    """Validate generated SQL against MySQL without reading business rows."""
    try:
        await fetch_all(f"EXPLAIN {sql}", tuple(params))
    except Exception as exc:
        raise RuntimeError(f"MySQL EXPLAIN rejected generated SQL: {exc}") from exc


def _flatten(expression: Any) -> List[Dict[str, Any]]:
    if not isinstance(expression, dict):
        return []
    if "boolean" in expression:
        return [
            leaf
            for child in expression.get("conditions", [])
            for leaf in _flatten(child)
        ]
    return [expression]


def _same_value(actual: Any, expected: Any) -> bool:
    if expected == "__CURRENT_DATE__":
        return False
    if isinstance(expected, str) and isinstance(actual, str):
        return actual.casefold() == expected.casefold()
    return actual == expected

def _actual_join_types(query: Dict[str, Any]) -> Dict[str, str]:
    """Map each joined table to its join type (LEFT/INNER) in the generated plan."""
    return {
        join.get("table"): join.get("join_type")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    }

def _required_match_tables(expression: Any) -> set[str]:
    """Tables that must have at least one matching row for a group to survive
    HAVING, e.g. HAVING COUNT(offer.id) >= 3. For these tables an INNER JOIN
    returns the same groups as a LEFT JOIN. Anything under an OR is ignored."""
    if not isinstance(expression, dict):
        return set()
    if "boolean" in expression:
        if expression["boolean"] != "AND":
            return set()
        tables: set[str] = set()
        for condition in expression.get("conditions", []):
            tables |= _required_match_tables(condition)
        return tables
    value = expression.get("value")
    if (
        expression.get("aggregate") == "COUNT"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (
            (expression.get("operator") == ">=" and value >= 1)
            or (expression.get("operator") == ">" and value >= 0)
        )
    ):
        return {expression.get("table")}
    return set()

def _evaluate_plan(
    case: Dict[str, Any],
    query: Dict[str, Any],
    relationships: Sequence[Dict[str, Any]] = (),
    schema_context: Dict[str, Any] | None = None,
) -> List[str]:
    failures: List[str] = []
    if case["expected_outcome"] == "refusal":
        failures.append("expected a safe refusal, but a query plan was generated")
    actual_tables = {query.get("table")}
    actual_tables.update(
        join.get("table")
        for join in query.get("joins", [])
        if isinstance(join, dict)
    )
    expected_tables = set(case["expected_tables"])
    if actual_tables != expected_tables:
        failures.append(
            f"tables expected={sorted(expected_tables)} actual={sorted(actual_tables)}"
        )

    actual_joins = {
        (
            join.get("table"),
            join.get("relationship", {}).get("constraint_name"),
            join.get("join_type"),
        )
        for join in query.get("joins", [])
        if isinstance(join, dict)
    }
    implied_not_null_tables = {
        item["table"]
        for item in case["expected_filters"]
        if item["operator"] == "IS NOT NULL"
    }
    is_top_n_ranking = case["expected_limit"] is not None and any(
        order["direction"] == "DESC" for order in case["expected_order_by"]
    )
    having_required_tables = _required_match_tables(query.get("having_expression"))
    for expected in case["expected_joins"]:
        signature = (
            expected["table"],
            expected["constraint_name"],
            expected["join_type"],
        )
        if signature in actual_joins:
            continue
        # INNER is equivalent to LEFT when the query drops unmatched rows anyway.
        inner_is_equivalent = (
            expected["join_type"] == "LEFT"
            and (
                expected["table"] in implied_not_null_tables
                or is_top_n_ranking
                or expected["table"] in having_required_tables
            )
            and (expected["table"], expected["constraint_name"], "INNER")
            in actual_joins
        )
        if not inner_is_equivalent:
            failures.append(f"missing join {signature}")
    if len(actual_joins) != len(case["expected_joins"]):
        failures.append(
            f"join count expected={len(case['expected_joins'])} actual={len(actual_joins)}"
        )

    actual_select_items = [
        item for item in query.get("select_items", []) if isinstance(item, dict)
    ]
    actual_selects = [
        (
            item.get("table"),
            item.get("column"),
            item.get("aggregate"),
            item.get("distinct", False),
        )
        for item in actual_select_items
    ]
    for expected in case["expected_selects"]:
        signature = (
            expected["table"],
            expected["column"],
            expected["aggregate"],
            expected["distinct"],
        )
        equivalent_count_star = (
            expected["aggregate"] == "COUNT"
            and expected["column"] != "*"
            and not expected["distinct"]
            and not query.get("joins")
            and any(
                item.get("table") == expected["table"]
                and item.get("column") == "*"
                and item.get("aggregate") == "COUNT"
                and not item.get("distinct", False)
                for item in actual_select_items
            )
        )
        if signature not in actual_selects and not equivalent_count_star:
            failures.append(f"missing select expression {signature}")
    expected_aggregate_count = sum(
        1 for item in case["expected_selects"] if item["aggregate"] is not None
    )
    actual_aggregate_count = sum(
        1 for item in actual_select_items if item.get("aggregate") is not None
    )
    if case["expected_selects"] and (
        actual_aggregate_count != expected_aggregate_count
        or len(actual_select_items) < len(case["expected_selects"])
    ):
        failures.append(
            f"selected expression count expected>={len(case['expected_selects'])} "
            f"(aggregates={expected_aggregate_count}) "
            f"actual={len(actual_select_items)} "
            f"(aggregates={actual_aggregate_count})"
        )
    if case["expected_row_listing"]:
        if not any(item.get("aggregate") is None for item in query.get("select_items", [])):
            failures.append("row-list request generated no plain row selection")

    where_leaves = _flatten(query.get("where_expression"))
    actual_join_types = _actual_join_types(query)
    implied_filter_count = 0
    for expected in case["expected_filters"]:
        is_null_check = expected["operator"] in {"IS NULL", "IS NOT NULL"}
        allowed_columns = {expected["column"]}
        if is_null_check and actual_join_types.get(expected["table"]) == "LEFT":
            primary_keys = {
                column.get("column")
                for column in (schema_context or {}).get(expected["table"], [])
                if isinstance(column, dict) and column.get("key") == "PRI"
            }
            relationship_columns = set()
            for join in query.get("joins", []):
                if (
                    not isinstance(join, dict)
                    or join.get("table") != expected["table"]
                    or join.get("join_type") != "LEFT"
                ):
                    continue
                selected_relationship = join.get("relationship", {})
                relationship = next(
                    (
                        edge
                        for edge in relationships
                        if edge.get("constraint_name")
                        == selected_relationship.get("constraint_name")
                        and edge.get("table") == selected_relationship.get("table")
                        and edge.get("referenced_table")
                        == selected_relationship.get("referenced_table")
                    ),
                    None,
                )
                if relationship is None:
                    continue
                for pair in relationship.get("column_pairs", []):
                    if not isinstance(pair, dict):
                        continue
                    if relationship.get("table") == expected["table"]:
                        relationship_columns.add(pair.get("column"))
                    if relationship.get("referenced_table") == expected["table"]:
                        relationship_columns.add(pair.get("referenced_column"))
            no_match_columns = primary_keys | relationship_columns
            if expected["column"] in no_match_columns:
                allowed_columns = no_match_columns
        found = any(
            leaf.get("table") == expected["table"]
            and leaf.get("column") in allowed_columns
            and leaf.get("operator") == expected["operator"]
            and (
                is_null_check
                or _same_value(leaf.get("value"), expected["value"])
            )
            for leaf in where_leaves
        )
        # An INNER JOIN already removes rows with no match, so
        # "IS NOT NULL" on that table is implied.
        if (
            not found
            and expected["operator"] == "IS NOT NULL"
            and actual_join_types.get(expected["table"]) == "INNER"
        ):
            implied_filter_count += 1
            continue
        if not found:
            failures.append(f"missing WHERE condition {expected}")
    expected_filter_count = len(case["expected_filters"]) - implied_filter_count
    if len(where_leaves) != expected_filter_count:
        failures.append(
            f"WHERE condition count expected={expected_filter_count} actual={len(where_leaves)}"
        )
    expected_boolean = case["expected_filter_boolean"]
    where_expression = query.get("where_expression")
    actual_boolean = (
        where_expression.get("boolean")
        if isinstance(where_expression, dict)
        else None
    )
    if expected_boolean and actual_boolean != expected_boolean:
        failures.append(
            f"WHERE connector expected={expected_boolean} "
            f"actual={actual_boolean}"
        )

    actual_groups = {
        (item.get("table"), item.get("column"))
        for item in query.get("group_by", [])
        if isinstance(item, dict)
    }
    for expected in case["expected_groups"]:
        if (expected["table"], expected["column"]) not in actual_groups:
            failures.append(f"missing GROUP BY {expected}")
    expected_groups = {
        (item["table"], item["column"]) for item in case["expected_groups"]
    }
    if actual_groups != expected_groups:
        failures.append(
            f"GROUP BY expected={sorted(expected_groups)} actual={sorted(actual_groups)}"
        )

    having_leaves = _flatten(query.get("having_expression"))
    for expected in case["expected_having"]:
        found = any(
            leaf.get("table") == expected["table"]
            and leaf.get("column") == expected["column"]
            and leaf.get("aggregate") == expected["aggregate"]
            and leaf.get("operator") == expected["operator"]
            and _same_value(leaf.get("value"), expected["value"])
            for leaf in having_leaves
        )
        if not found:
            failures.append(f"missing HAVING condition {expected}")
    if len(having_leaves) != len(case["expected_having"]):
        failures.append(
            f"HAVING condition count expected={len(case['expected_having'])} actual={len(having_leaves)}"
        )

    actual_order = query.get("order_by", [])
    for expected in case["expected_order_by"]:
        order_matches = any(
            order.get("direction") == expected["direction"]
            and (
                order.get("column") == expected["column"]
                or any(
                    item.get("alias") == order.get("column")
                    and any(
                        select["aggregate"] is not None
                        and item.get("table") == select["table"]
                        and item.get("column") == select["column"]
                        and item.get("aggregate") == select["aggregate"]
                        for select in case["expected_selects"]
                    )
                    for item in actual_select_items
                )
            )
            for order in actual_order
            if isinstance(order, dict)
        )
        if not order_matches:
            failures.append(f"missing ORDER BY {expected}")
    if case["expected_order_by"] and len(actual_order) != len(
        case["expected_order_by"]
    ):
        failures.append(
            f"ORDER BY expression count expected={len(case['expected_order_by'])} "
            f"actual={len(actual_order)}"
        )

    if case["expected_limit"] is not None:
        if query.get("limit") != case["expected_limit"]:
            failures.append(
                f"user LIMIT {case['expected_limit']} is absent from generated query plan"
            )
    if case["expected_round_digits"] is not None:
        round_digits = [
            item.get("round_digits")
            for item in actual_select_items
            if item.get("aggregate") in {"SUM", "AVG", "MIN", "MAX"}
        ]
        if not round_digits or any(
            digits != case["expected_round_digits"] for digits in round_digits
        ):
            failures.append(
                f"ROUND(..., {case['expected_round_digits']}) is absent from generated query plan"
            )
    return failures


def _retrieval_metrics(
    selected: Sequence[str], expected: Sequence[str]
) -> Tuple[float, float, float]:
    selected_set = set(selected)
    expected_set = set(expected)
    if not selected_set:
        return 0.0, 0.0, 0.0
    precision = len(selected_set & expected_set) / len(selected_set)
    recall = len(selected_set & expected_set) / len(expected_set) if expected_set else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return precision, recall, f1

def _parse_case_numbers(spec: str) -> set[int]:
    """Turn '7,19-25' into {7, 19, 20, 21, 22, 23, 24, 25}."""
    numbers: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            numbers.update(range(int(start), int(end) + 1))
        else:
            numbers.add(int(part))
    return numbers

async def _run_case(
    case: Dict[str, Any], *, execute: bool = False
) -> Dict[str, Any]:
    started = time.perf_counter()
    row: Dict[str, Any] = {
        "case_number": case["number"],
        "category": case["category"],
        "question": case["question"],
        "expected_outcome": case["expected_outcome"],
        "rewritten_question": "",
        "total_ms": "",
        "setup_ms": "",
        "classification_ms": "",
        "schema_grade_ms": "",
        "retrieval_calls_ms": "[]",
        "generation_ms": "",
        "business_rules_ms": "",
        "sql_validation_ms": "",
        "schema_attempts": 0,
        "generation_status": "not_run",
        "retrieved_tables_by_attempt": "[]",
        "selected_schema_tables": "[]",
        "expected_tables": _json(case["expected_tables"]),
        "raw_retrieval_table_precision": 0.0,
        "raw_retrieval_table_recall": 0.0,
        "raw_retrieval_table_f1": 0.0,
        "grounded_schema_table_precision": 0.0,
        "grounded_schema_table_recall": 0.0,
        "grounded_schema_table_f1": 0.0,
        "required_relationship_recall": 0.0,
        "verified_relationships_available": "[]",
        "matched_business_rules": "[]",
        "generated_base_table": "",
        "generated_joins": "[]",
        "generated_select_items": "[]",
        "generated_filters": "[]",
        "generated_group_by": "[]",
        "generated_having": "[]",
        "generated_order_by": "[]",
        "generated_query_plan": "",
        "compiled_sql": "",
        "sql_parameters": "[]",
        "safety_validation": "not_run",
        "explain_validation": "not_run",
        "plan_match": "not_run",
        "plan_check_failures": "[]",
        "executed_row_count": "",
        "executed_preview": "[]",
        "truncated": "",
        "no_data_reason": "",
        "execution_answer_type": "",
        "execution_message": "",
        "execution_check": "not_run",
        "reference_note": case["reference_note"],
        "error": "",
        "error_type": "",
        "error_bases": "",
        "error_cause": "",
        "rule_id": "",
        "error_stage": "",
    }

    query: Dict[str, Any] | None = None
    try:
        stage = time.perf_counter()
        classification = await classify_and_rewrite(case["question"])
        row["classification_ms"] = round((time.perf_counter() - stage) * 1000, 3)
        if classification.get("intent") != "data":
            raise RuntimeError(
                f"Classifier routed this data benchmark case as "
                f"{classification.get('intent')!r}."
            )
        row["rewritten_question"] = classification["rewritten_question"]
        question = classification["rewritten_question"]
        preflight_refusals(question, case["question"])

        retrieval_calls: List[Dict[str, Any]] = []
        original_retrieve = grade_sample.retrieve_relevant_tables

        async def timed_retrieve(query_text: str, top_k: int = 6) -> List[str]:
            retrieve_started = time.perf_counter()
            tables = await original_retrieve(query_text, top_k=top_k)
            retrieval_calls.append({
                "top_k": top_k,
                "tables": tables,
                "milliseconds": round(
                    (time.perf_counter() - retrieve_started) * 1000, 3
                ),
            })
            return tables

        stage = time.perf_counter()
        grade_sample.retrieve_relevant_tables = timed_retrieve
        try:
            try:
                grounded = await grade_schema_sufficiency(question)
            finally:
                row["schema_grade_ms"] = round(
                    (time.perf_counter() - stage) * 1000, 3
                )
                row["retrieved_tables_by_attempt"] = _json(retrieval_calls)
                row["schema_attempts"] = len(retrieval_calls)
        finally:
            grade_sample.retrieve_relevant_tables = original_retrieve
        row["schema_attempts"] = grounded["attempts"]
        selected_tables = list(grounded["schema"])
        row["selected_schema_tables"] = _json(selected_tables)
        raw_tables = retrieval_calls[0]["tables"] if retrieval_calls else []
        raw_precision, raw_recall, raw_f1 = _retrieval_metrics(
            raw_tables, case["expected_tables"]
        )
        row["raw_retrieval_table_precision"] = round(raw_precision, 4)
        row["raw_retrieval_table_recall"] = round(raw_recall, 4)
        row["raw_retrieval_table_f1"] = round(raw_f1, 4)
        precision, recall, f1 = _retrieval_metrics(
            selected_tables, case["expected_tables"]
        )
        row["grounded_schema_table_precision"] = round(precision, 4)
        row["grounded_schema_table_recall"] = round(recall, 4)
        row["grounded_schema_table_f1"] = round(f1, 4)
        required_relationships = {
            join["constraint_name"] for join in case["expected_joins"]
        }
        available_relationships = {
            relationship.get("constraint_name")
            for relationship in grounded["relationships"]
        }
        row["verified_relationships_available"] = _json(
            sorted(available_relationships)
        )
        row["required_relationship_recall"] = round(
            len(required_relationships & available_relationships)
            / len(required_relationships)
            if required_relationships
            else 1.0,
            4,
        )

        stage = time.perf_counter()
        rules = find_relevant_rules(question)
        row["business_rules_ms"] = round((time.perf_counter() - stage) * 1000, 3)
        row["matched_business_rules"] = _json(
            [rule.get("term") for rule in rules]
        )
        generation_started = time.perf_counter()
        try:
            query = await generate_query(
                question,
                grounded["schema"],
                rules,
                grounded["relationships"],
                case["question"],
            )
        finally:
            row["generation_ms"] = round(
                (time.perf_counter() - generation_started) * 1000, 3
            )
        row["generation_status"] = "generated"
        row["generated_base_table"] = query.get("table", "")
        row["generated_joins"] = _json(query.get("joins", []))
        row["generated_select_items"] = _json(query.get("select_items", []))
        row["generated_filters"] = _json(query.get("where_expression"))
        row["generated_group_by"] = _json(query.get("group_by", []))
        row["generated_having"] = _json(query.get("having_expression"))
        row["generated_order_by"] = _json(query.get("order_by", []))
        row["generated_query_plan"] = _json(query)

        plan_failures = _evaluate_plan(
            case, query, grounded["relationships"], grounded["schema"]
        )
        if plan_failures:
            for alternative in case.get("alternatives", []):
                if not _evaluate_plan(
                    alternative, query, grounded["relationships"], grounded["schema"]
                ):
                    plan_failures = []
                    break
        row["plan_check_failures"] = _json(plan_failures)
        row["plan_match"] = "yes" if not plan_failures else "no"

        stage = time.perf_counter()
        try:
            safe_query = run_safety_check(
                query,
                valid_tables=list(grounded["schema"]),
                schema_context=grounded["schema"],
                relationships=grounded["relationships"],
            )
        finally:
            row["sql_validation_ms"] = round(
                (time.perf_counter() - stage) * 1000, 3
            )
        row["compiled_sql"] = safe_query["sql"]
        row["sql_parameters"] = _json(safe_query.get("params", []))
        row["safety_validation"] = "passed"
        await _explain_query(
            safe_query["sql"],
            safe_query.get("params", []),
        )
        row["explain_validation"] = "passed"
        if execute:
            execution = await run_query({
                **safe_query,
                "columns": _execution_columns(query, grounded["schema"]),
            })
            answer = _build_result_answer(execution, query)
            row["executed_row_count"] = execution["row_count"]
            row["executed_preview"] = _json(
                _safe_execution_preview(execution["results"])
            )
            row["truncated"] = execution["truncated"]
            row["no_data_reason"] = execution.get("no_data_reason") or ""
            row["execution_answer_type"] = answer["answer_type"]
            row["execution_message"] = answer.get("message", "")
            row["execution_check"] = _check_execution_expectation(
                case["number"], execution
            )
    except Exception as exc:
        logger.exception("Benchmark case %s failed", case["number"])
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["error_type"] = type(exc).__name__
        row["error_bases"] = ",".join(c.__name__ for c in type(exc).__mro__)
        row["error_cause"] = type(exc.__cause__).__name__ if exc.__cause__ else ""
        row["rule_id"] = getattr(exc, "rule_id", None) or ""
        row["error_stage"] = getattr(exc, "stage", None) or ""
        if execute and row["explain_validation"] == "passed":
            row["execution_check"] = "failed"
        if query is None:
            row["generation_status"] = (
                "refused"
                if isinstance(exc, (UnanswerableQuestionError, MissingRelationshipError))
                else "failed"
            )
        if isinstance(exc, SafetyViolationError):
            row["safety_validation"] = "failed"
        if isinstance(exc, RuntimeError) and str(exc).startswith(
            "MySQL EXPLAIN rejected generated SQL:"
        ):
            row["explain_validation"] = "failed"
        if isinstance(exc, (UnanswerableQuestionError, MissingRelationshipError)):
            row["plan_match"] = (
                "yes"
                if case["expected_outcome"] == "refusal"
                else "no"
            )
        elif isinstance(exc, (GradeSampleError, QueryGenerationError)):
            row["plan_match"] = "no"
    finally:
        row["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        retrieval_json = json.loads(row["retrieved_tables_by_attempt"])
        row["retrieval_calls_ms"] = _json(
            [call["milliseconds"] for call in retrieval_json]
        )
        if query is None and row["plan_match"] == "not_run":
            row["plan_match"] = "no"
        calls = json.loads(row["retrieved_tables_by_attempt"])
        if calls:
            raw_tables = calls[0]["tables"]
            raw_precision, raw_recall, raw_f1 = _retrieval_metrics(
                raw_tables, case["expected_tables"]
            )
            row["raw_retrieval_table_precision"] = round(raw_precision, 4)
            row["raw_retrieval_table_recall"] = round(raw_recall, 4)
            row["raw_retrieval_table_f1"] = round(raw_f1, 4)
    return row


async def _run(
    output_path: Path,
    selected: set[int] | None = None,
    *,
    execute: bool = False,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    setup_started = time.perf_counter()
    await connect_to_mysql()
    try:
        if execute:
            await connect_to_read_replica()
        await refresh_schema_cache()
        await build_schema_index(get_cached_schema())
        setup_ms = round((time.perf_counter() - setup_started) * 1000, 3)

        rows = []
        cases_to_run = [
            case for case in CASES
            if selected is None or case["number"] in selected
        ]
        if not cases_to_run:
            raise SystemExit(f"No benchmark cases match {sorted(selected or [])}.")
        for case in cases_to_run:
            logger.info("Benchmarking case %d/%d", case["number"], len(CASES))
            row = await _run_case(case, execute=execute)
            row["setup_ms"] = setup_ms
            rows.append(row)

        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        print(f"Wrote {len(rows)} diagnostic rows to {output_path}")
        if execute:
            print("Generated SQL was executed against the read-only replica.")
            print("Preview values containing identifiers, contact details, or financial data were redacted.")
        else:
            print("Generated SQL was safety-validated but never executed.")
    finally:
        await close_mysql_connection()

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("query_generation_benchmark_execution.csv"),
        help="CSV destination (default: app/tests/query_generation_benchmark_execution.csv)",
    )
    parser.add_argument(
        "--cases",
        type=str,
        default=None,
        help="Run only these case numbers, e.g. '7,19-25' (default: all)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute safety-validated SQL on the read-only replica and record answer diagnostics.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    selected = _parse_case_numbers_impl(args.cases) if args.cases else None
    asyncio.run(_run(args.output.resolve(), selected, execute=args.execute))

if __name__ == "__main__":
    main()
