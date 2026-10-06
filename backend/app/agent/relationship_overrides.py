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

# The following logical joins were confirmed by the database owner through
# manual query testing. MySQL does not enforce these foreign keys.
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
        "constraint_name": "reviewed_shop_address_shop_id",
        "table": "shop_address",
        "referenced_table": "shop_details",
        "cardinality": "many_to_one",
        "verification": "reviewed_manual_query",
        "column_pairs": [
            {"column": "shop_id", "referenced_column": "id"},
        ],
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
)
