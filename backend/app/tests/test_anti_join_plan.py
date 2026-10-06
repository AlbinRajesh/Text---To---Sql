"""Phase 4: 'users with no booking' is expressible with the verified relationship."""

from app.agent.nodes.generate_query import _validate_llm_output

SCHEMA = {
    "users": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "plan_id", "data_type": "bigint", "key": ""},
    ],
    "bookings": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "requester_id", "data_type": "bigint", "key": ""},
    ],
}
RELATIONSHIPS = [{
    "constraint_name": "reviewed_bookings_requester_id",
    "table": "bookings",
    "referenced_table": "users",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [{"column": "requester_id", "referenced_column": "id"}],
}]


def test_users_with_no_plan_and_no_booking_plan_is_valid():
    plan = {
        "answerable": True,
        "reasoning": "t",
        "table": "users",
        "joins": [{
            "table": "bookings",
            "join_type": "LEFT",
            "relationship": {
                "constraint_name": "reviewed_bookings_requester_id",
                "table": "bookings",
                "referenced_table": "users",
            },
        }],
        "select_items": [{
            "table": "users", "column": "id",
            "aggregate": "COUNT", "distinct": True, "alias": "user_count",
        }],
        "where_expression": {
            "boolean": "AND",
            "conditions": [
                {"table": "users", "column": "plan_id", "operator": "IS NULL"},
                {"table": "bookings", "column": "id", "operator": "IS NULL"},
            ],
        },
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": {"table": "users", "column": "id"},
        "order_by": [],
    }
    normalized = _validate_llm_output(plan, SCHEMA, RELATIONSHIPS)
    assert normalized["joins"][0]["join_type"] == "LEFT"
    assert normalized["count_entity"] == {"table": "users", "column": "id"}
