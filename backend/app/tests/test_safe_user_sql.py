import pytest
import sqlglot
from sqlglot import exp

from app.services.safe_user_sql import (
    UserSQLValidationError,
    validate_user_select,
)


SCHEMA = {
    "shop_details": [
        {"column": "id"},
        {"column": "name"},
        {"column": "category_id"},
        {"column": "status"},
    ],
    "offer": [
        {"column": "shop_id"},
        {"column": "offer_heading"},
        {"column": "actual_price"},
        {"column": "offer_price"},
    ],
    "categories": [
        {"column": "id"},
        {"column": "categoriesname"},
    ],
    "plans": [
        {"column": "id"},
        {"column": "name"},
    ],
    "customer": [
        {"column": "id"},
    ],
    "users": [
        {"column": "id"},
        {"column": "phone"},
    ],
}


def test_allows_bounded_query_and_ordering_by_selected_alias():
    query = """
        SELECT s.name AS shop_name, o.offer_heading,
               (o.actual_price - o.offer_price) AS discount
        FROM shop_details s
        JOIN offer o ON s.id = o.shop_id
        WHERE s.status = 1
        ORDER BY discount DESC
        LIMIT 1; --can you run it
    """
    validated = validate_user_select(query, SCHEMA)
    parsed = sqlglot.parse_one(validated, read="mysql")

    assert isinstance(parsed, exp.Select)
    assert parsed.args["limit"].expression.this == "1"


def test_allows_user_run_instruction_after_select():
    validated = validate_user_select(
        "SELECT s.name FROM shop_details s LIMIT 1;-run this",
        SCHEMA,
    )

    assert sqlglot.parse_one(validated, read="mysql").args["limit"].expression.this == "1"


def test_limits_unbounded_select_and_caps_large_limit():
    unbounded = validate_user_select(
        "SELECT s.name FROM shop_details s",
        SCHEMA,
    )
    capped = validate_user_select(
        "SELECT s.name FROM shop_details s LIMIT 9999",
        SCHEMA,
    )

    assert sqlglot.parse_one(unbounded, read="mysql").args["limit"].expression.this == "201"
    assert sqlglot.parse_one(capped, read="mysql").args["limit"].expression.this == "201"


def test_unqualified_column_ignores_unreferenced_tables():
    query = validate_user_select(
        "SELECT name FROM shop_details",
        SCHEMA,
    )
    assert "SELECT name FROM shop_details" in query


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM shop_details",
        "SELECT s.name FROM shop_details s; SELECT s.id FROM shop_details s",
        "SELECT s.* FROM shop_details s",
        "SELECT phone FROM customer",
        "SELECT unknown_column FROM shop_details",
        "SELECT SLEEP(10) FROM shop_details",
        "SELECT CURRENT_USER() AS current_user FROM shop_details",
        "SELECT @@version AS server_version FROM shop_details",
        "SELECT @session_value AS session_value FROM shop_details",
        "SELECT s.name FROM shop_details s LIMIT 0",
    ],
)
def test_rejects_unsafe_or_invalid_sql(query):
    with pytest.raises(UserSQLValidationError):
        validate_user_select(query, SCHEMA)


def test_unqualified_columns_are_checked_only_against_referenced_tables():
    with pytest.raises(UserSQLValidationError, match="unknown or ambiguous"):
        validate_user_select(
            "SELECT id FROM shop_details s JOIN categories c ON s.category_id = c.id",
            SCHEMA,
        )


def test_rejects_personal_data_column_even_when_qualified():
    with pytest.raises(UserSQLValidationError, match="Personal contact"):
        validate_user_select(
            "SELECT u.phone FROM users u",
            SCHEMA,
        )


def test_allows_hour_and_time_filters_on_verified_timestamp_columns():
    schema = {
        **SCHEMA,
        "views": [
            {"column": "id", "data_type": "bigint"},
            {"column": "created_at", "data_type": "timestamp"},
        ],
    }
    validated = validate_user_select(
        "SELECT HOUR(v.created_at) AS view_hour FROM views v "
        "WHERE TIME(v.created_at) BETWEEN '02:00:00' AND '04:00:00'",
        schema,
    )

    assert "HOUR(v.created_at)" in validated
    assert "TIME(v.created_at) BETWEEN '02:00:00' AND '04:00:00'" in validated


def test_rejects_hour_and_time_functions_on_non_date_columns():
    schema = {**SCHEMA, "views": [{"column": "offer_id", "data_type": "bigint"}]}

    with pytest.raises(UserSQLValidationError, match="verified date/time column"):
        validate_user_select(
            "SELECT HOUR(v.offer_id) AS view_hour FROM views v",
            schema,
        )
