"""
test_safety_check.py — manual smoke test for safety_check.py (Node 7, AST version)

No database or LLM calls — pure logic test using hand-built query dicts,
including deliberately dangerous ones that MUST be rejected.

Run from backend/:
    python -m app.tests.test_safety_check
"""

from app.agent.nodes.safety_check import run_safety_check, SafetyViolationError

VALID_TABLES = ["accounts", "customers", "transactions"]

# Fake cached schema shape — must match Node 3's real output shape:
# {table: [{"column": ..., "data_type": ..., "nullable": ..., "key": ...}]}
SCHEMA_CONTEXT = {
    "accounts": [
        {"column": "id", "data_type": "int", "nullable": False, "key": "PRI"},
        {"column": "balance", "data_type": "decimal", "nullable": False, "key": ""},
    ],
    "transactions": [
        {"column": "transaction_code", "data_type": "varchar", "nullable": False, "key": ""},
        {"column": "amount", "data_type": "decimal", "nullable": False, "key": ""},
    ],
    "customers": [
        {"column": "id", "data_type": "int", "nullable": False, "key": "PRI"},
        {"column": "name", "data_type": "varchar", "nullable": False, "key": ""},
    ],
}

# (label, query_dict, should_pass)
TEST_CASES = [
    (
        "Valid select query",
        {
            "table": "accounts",
            "select_items": [{"column": "id"}, {"column": "balance"}],
            "where_conditions": [{"column": "balance", "operator": ">", "value": 10000}],
            "combine_with": "AND"
        },
        True,
    ),
    (
        "Valid aggregate query",
        {
            "table": "transactions",
            "select_items": [
                {"column": "transaction_code"},
                {"column": "amount", "aggregate": "SUM", "alias": "total"}
            ],
            "group_by": ["transaction_code"]
        },
        True,
    ),
    (
        "Unknown table",
        {
            "table": "admin_secrets",
            "select_items": [{"column": "id"}]
        },
        False,
    ),
    (
        "Dangerous identifier (SQL injection attempt in column name)",
        {
            "table": "accounts",
            "select_items": [{"column": "id; DROP TABLE users;--"}]
        },
        False,
    ),
    (
        "Disallowed operator",
        {
            "table": "accounts",
            "select_items": [{"column": "id"}],
            "where_conditions": [{"column": "balance", "operator": "=>", "value": 100}]
        },
        False,
    ),
    (
        "Missing select_items (must fail)",
        {
            "table": "accounts",
            "where_conditions": [{"column": "id", "operator": "=", "value": 1}]
        },
        False,
    ),
    (
        "Dangerous aggregate function",
        {
            "table": "accounts",
            "select_items": [{"column": "id", "aggregate": "EXEC"}]
        },
        False,
    ),
    (
        "Column not in real schema (AST layer must catch this even if build layer misses it)",
        {
            "table": "accounts",
            "select_items": [{"column": "password_hash"}]  # not in SCHEMA_CONTEXT["accounts"]
        },
        False,
    ),
]


def main() -> None:
    passed = 0
    failed = 0

    for label, query, should_pass in TEST_CASES:
        print(f"Case: {label}")
        try:
            result = run_safety_check(query, valid_tables=VALID_TABLES, schema_context=SCHEMA_CONTEXT)
            if should_pass:
                print(f"   OK — passed safety check. SQL: {result['sql']} | Params: {result['params']}\n")
                passed += 1
            else:
                print(f"   FAILED — this should have been rejected but wasn't. Result: {result}\n")
                failed += 1
        except SafetyViolationError as e:
            if not should_pass:
                print(f"   OK — correctly rejected: {e}\n")
                passed += 1
            else:
                print(f"   FAILED — this should have passed but was rejected: {e}\n")
                failed += 1

    print(f"Summary: {passed} passed, {failed} failed out of {len(TEST_CASES)}")


if __name__ == "__main__":
    main()