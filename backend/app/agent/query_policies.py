"""Shared checks for query shapes whose aggregates remain valid across joins."""

from typing import Any, Dict, List


def is_safe_per_shop_review_average(
    base_table: str,
    aggregate_owner: str,
    aggregate_column: str,
    group_by: List[Dict[str, Any]],
    joins: List[Dict[str, Any]],
    where_expression: Dict[str, Any] | None,
) -> bool:
    """Allow per-shop AVG(review.rating) with shop-owned dimensions.

    Each review row for a given shop is repeated the same number of times by
    joins from that shop to its dimensions. AVG remains invariant within each
    shop group. Address joins are accepted only when restricted to the default
    address; other one-to-many joins are not.
    """
    if (base_table, aggregate_owner, aggregate_column) != (
        "reviews", "reviews", "rating"
    ):
        return False
    if not any(
        isinstance(group, dict)
        and group.get("table") == "shop_details"
        and group.get("column") == "id"
        for group in group_by
    ):
        return False

    review_to_shop = False
    uses_default_address = False
    for join in joins:
        table = join.get("table")
        relationship = join.get("relationship") or {}
        left = relationship.get("table")
        right = relationship.get("referenced_table")
        cardinality = relationship.get("cardinality")

        if (
            table == "shop_details"
            and left == "reviews"
            and right == "shop_details"
            and cardinality == "many_to_one"
        ):
            review_to_shop = True
            continue

        if (
            table == "shop_address"
            and left == "shop_address"
            and right == "shop_details"
            and cardinality == "many_to_one"
        ):
            uses_default_address = True
            continue

        if (
            table != right
            or left != "shop_details"
            or cardinality != "many_to_one"
        ):
            return False

    if not review_to_shop:
        return False
    if not uses_default_address:
        return True

    def has_default_address_filter(expression: Any) -> bool:
        if not isinstance(expression, dict):
            return False
        conditions = expression.get("conditions")
        if isinstance(conditions, list):
            return (
                expression.get("boolean") == "AND"
                and any(has_default_address_filter(item) for item in conditions)
            )
        return (
            expression.get("table") == "shop_address"
            and expression.get("column") == "is_default"
            and expression.get("operator") == "="
            and type(expression.get("value")) is int
            and expression.get("value") == 1
        )

    return has_default_address_filter(where_expression)
