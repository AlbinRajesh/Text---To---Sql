"""Reviewed logical relationships for schemas that omit FK constraints.

Add only relationships confirmed from the application's data model. Entries
must use the same shape as schema_grounding.fetch_relationships_from_information_schema:

{
    "constraint_name": "reviewed_shop_category",
    "table": "shop_details",
    "referenced_table": "categories",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [
        {"column": "category_id", "referenced_column": "id"},
    ],
}

Every table and column is checked against the live schema at startup.
Cardinality is from `table` to `referenced_table`. Similar column names are
never treated as evidence of a relationship.
"""

from typing import Any, Dict, Tuple

# These logical joins are reviewed against application semantics and checked
# against live data where available. MySQL does not enforce these foreign keys.
CURATED_RELATIONSHIP_OVERRIDES: Tuple[Dict[str, Any], ...] = (
    {
        "constraint_name": "reviewed_shop_details_category_id",
        "table": "shop_details",
        "referenced_table": "categories",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "category_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_shop_details_subcategory_id",
        "table": "shop_details",
        "referenced_table": "subcategories",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "subcategory_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_reviews_shop_id",
        "table": "reviews",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "shop_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_reviews_user_id",
        "table": "reviews",
        "referenced_table": "users",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "user_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_views_user_id",
        "table": "views",
        "referenced_table": "users",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "user_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_views_shop_id",
        "table": "views",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "shop_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_bookings_shop_id",
        "table": "bookings",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "shop_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_offer_shop_id",
        "table": "offer",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "shop_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_offer_items_view_shop_id",
        "table": "offer_items_view",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
    },
    {
        "constraint_name": "reviewed_shop_address_shop_id",
        "table": "shop_address",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "shop_id", "referenced_column": "id"},
        ],
        # Business rule: a shop has exactly one default address. A query that
        # filters is_default = 1 gets at most one row per shop from this table,
        # so joining it below shop_details cannot multiply numeric aggregates.
        "single_row_when": {"column": "is_default", "value": 1},
    },
    {
        "constraint_name": "reviewed_bookings_requester_id",
        "table": "bookings",
        "referenced_table": "users",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "requester_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_shop_details_partner_id",
        "table": "shop_details",
        "referenced_table": "users",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "partner_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_bookings_offer_id",
        "table": "bookings",
        "referenced_table": "offer",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "offer_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_offer_service_id",
        "table": "offer",
        "referenced_table": "service",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "service_id", "referenced_column": "id"},
        ],
    },
    {
        # Confirmed against current invoice data: every plan_id resolves to a
        # plan, and every paid invoice has a plan. MySQL supports this int-to-
        # bigint join; subscriptions.plan_id is text and is not the relationship.
        "constraint_name": "reviewed_invoices_plan_id",
        "table": "invoices",
        "referenced_table": "plans",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "plan_id", "referenced_column": "id"},
        ],
    },
    {
        "constraint_name": "reviewed_users_plan_id",
        "table": "users",
        "referenced_table": "plans",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "plan_id", "referenced_column": "id"},
        ],
    },
)

def _top_level_and_terms(where_expression):
    """Terms that must ALL be true. Anything under OR is excluded."""
    if not isinstance(where_expression, dict):
        return []
    if where_expression.get("boolean") == "AND":
        terms = []
        for condition in where_expression.get("conditions", []):
            terms.extend(_top_level_and_terms(condition))
        return terms
    if "column" in where_expression:
        return [where_expression]
    return []


def join_is_anti_join_safe(
    target_table,
    relationship,
    where_expression,
    join_type,
    schema_context,
):
    """True for a verified child LEFT JOIN eliminated by its PK IS NULL filter."""
    if (
        not isinstance(relationship, dict)
        or relationship.get("cardinality", "many_to_one") != "many_to_one"
        or target_table != relationship.get("table")
        or str(join_type).upper() != "LEFT"
        or not isinstance(schema_context, dict)
    ):
        return False
    primary_keys = [
        column.get("column")
        for column in schema_context.get(target_table, [])
        if isinstance(column, dict) and column.get("key") == "PRI"
    ]
    return len(primary_keys) == 1 and any(
        term.get("table") == target_table
        and term.get("column") == primary_keys[0]
        and term.get("operator") == "IS NULL"
        for term in _top_level_and_terms(where_expression)
    )


def join_is_fanout_safe(
    target_table,
    relationship,
    where_expression,
    join_conditions=None,
    join_type=None,
    schema_context=None,
):
    """True if joining target_table cannot multiply rows of a numeric aggregate.

    Safe when (a) the join goes up to a many-to-one parent, or (b) it goes down
    to a child table whose verified relationship declares `single_row_when` and
    the query has that exact top-level AND filter, or (c) a left anti-join to a
    child table filtered by its primary key IS NULL.
    """
    if not isinstance(relationship, dict):
        return False
    if relationship.get("cardinality", "many_to_one") != "many_to_one":
        return False
    if target_table == relationship.get("referenced_table"):
        return True
    if join_is_anti_join_safe(
        target_table, relationship, where_expression, join_type, schema_context
    ):
        return True
    rule = relationship.get("single_row_when")
    if target_table != relationship.get("table") or not isinstance(rule, dict):
        return False
    terms = _top_level_and_terms(where_expression)
    for condition in join_conditions or []:
        terms.extend(_top_level_and_terms(condition))
    return any(
        term.get("table") == relationship["table"]
        and term.get("column") == rule["column"]
        and term.get("operator") == "="
        and not isinstance(term.get("value"), bool)
        and str(term.get("value")) == str(rule["value"])
        for term in terms
    )


def is_verified_many_to_one_parent(base_table, target_table, joins):
    """True when target_table is reachable by verified many-to-one joins."""
    if not isinstance(base_table, str) or not isinstance(target_table, str):
        return False
    if base_table == target_table:
        return True
    parents_by_child = {}
    for join in (joins if isinstance(joins, list) else []):
        if not isinstance(join, dict):
            continue
        relationship = join.get("relationship")
        if (
            not isinstance(relationship, dict)
            or relationship.get("cardinality", "many_to_one") != "many_to_one"
            or join.get("table") != relationship.get("referenced_table")
        ):
            continue
        child = relationship.get("table")
        parent = relationship.get("referenced_table")
        if isinstance(child, str) and isinstance(parent, str):
            parents_by_child.setdefault(child, set()).add(parent)

    pending = [base_table]
    visited = {base_table}
    while pending:
        for parent in parents_by_child.get(pending.pop(), set()):
            if parent == target_table:
                return True
            if parent not in visited:
                visited.add(parent)
                pending.append(parent)
    return False


def child_aggregate_is_safe(
    base_table,
    aggregate_table,
    group_by,
    joins,
    schema_context,
    where_expression,
):
    """Allow one direct child aggregate per base-PK group, plus safe joins."""
    if not isinstance(base_table, str) or not isinstance(aggregate_table, str):
        return False
    primary_keys = [
        column.get("column")
        for column in schema_context.get(base_table, [])
        if isinstance(column, dict) and column.get("key") == "PRI"
    ]
    if len(primary_keys) != 1 or not isinstance(group_by, list) or not group_by:
        return False
    grouped_columns = [
        (group.get("table"), group.get("column"))
        for group in group_by
        if isinstance(group, dict)
    ]
    base_columns = {
        column.get("column")
        for column in schema_context.get(base_table, [])
        if isinstance(column, dict)
    }
    if (
        len(grouped_columns) != len(group_by)
        or (base_table, primary_keys[0]) not in grouped_columns
        or any(
            table != base_table or column not in base_columns
            for table, column in grouped_columns
        )
    ):
        return False
    if not isinstance(joins, list):
        return False

    child_joins = [
        join
        for join in joins
        if isinstance(join, dict)
        and join.get("table") == aggregate_table
        and isinstance(join.get("relationship"), dict)
        and join["relationship"].get("table") == aggregate_table
        and join["relationship"].get("referenced_table") == base_table
        and join["relationship"].get("cardinality", "many_to_one")
        == "many_to_one"
    ]
    if len(child_joins) != 1:
        return False

    return all(
        join is child_joins[0]
        or (
            isinstance(join, dict)
            and join_is_fanout_safe(
                join.get("table"),
                join.get("relationship"),
                where_expression,
                join.get("conditions"),
                join.get("join_type"),
                schema_context,
            )
        )
        for join in joins
    )


def child_aggregate_is_safe_plan(
    base_table,
    aggregate_table,
    group_by,
    joins,
    schema_context,
    where_expression,
    relationships,
):
    """Apply child-aggregate checks using verified catalog edges."""
    catalog_joins = []
    for join in joins:
        if not isinstance(join, dict):
            return False
        claimed = join.get("relationship")
        if not isinstance(claimed, dict):
            return False
        matches = [
            edge
            for edge in relationships or []
            if edge.get("constraint_name") == claimed.get("constraint_name")
            and {
                edge.get("table"),
                edge.get("referenced_table"),
            } == {
                claimed.get("table"),
                claimed.get("referenced_table"),
            }
        ]
        if len(matches) != 1:
            return False
        catalog_joins.append({
            **join,
            "relationship": matches[0],
        })
    return child_aggregate_is_safe(
        base_table,
        aggregate_table,
        group_by,
        catalog_joins,
        schema_context,
        where_expression,
    )
