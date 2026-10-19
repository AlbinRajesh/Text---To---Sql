"""
test_safety_check.py — direct unit tests for Node 7 (safety_check.py)

Unlike test_graph.py, this bypasses Node 6 (generate_query) entirely and
calls safety_check.py's functions directly. This is the only reliable way
to prove the safety layer works, because:

  - Node 6 is ALSO schema-constrained, so it rarely produces a malicious
    structure on its own — attacks sent through the full pipeline get
    neutralized before they ever reach safety_check.py, proving nothing
    about safety_check.py itself.
  - The AST layer (_ast_verify) exists specifically as a defense against
    a BUG in the build layer (_build_sql) — so to prove it works, we must
    test it independently of the build layer, not through it.

Two groups of tests:
  1. run_safety_check() with a hand-crafted malicious QUERY DICT — proves
     the build layer (Layer 1) rejects bad structured input.
  2. _ast_verify() called directly with hand-crafted malicious SQL STRINGS
     that bypass the builder — proves the AST layer (Layer 2) independently
     catches what it's designed to catch, even if Layer 1 had a bug.

Run from backend/:
    python -m app.tests.test_safety_check
"""

from app.agent.nodes.safety_check import (
    run_safety_check,
    _ast_verify,
    SafetyViolationError,
)

# Minimal fake schema — enough to exercise the checks without hitting a real DB.
FAKE_SCHEMA = {
    "plans": [
        {"column": "id", "data_type": "int"},
        {"column": "name", "data_type": "varchar"},
        {"column": "price", "data_type": "decimal"},
    ],
}
VALID_TABLES = list(FAKE_SCHEMA.keys())


def _expect_rejection(label: str, fn, *args, **kwargs) -> None:
    """Runs fn(*args, **kwargs) and asserts it raises SafetyViolationError."""
    try:
        result = fn(*args, **kwargs)
        print(f"[FAIL] {label}: expected rejection, but got a result: {result}")
    except SafetyViolationError as e:
        print(f"[PASS] {label}: correctly rejected -> {e}")
    except Exception as e:
        print(f"[FAIL] {label}: rejected, but with the WRONG exception type "
              f"({type(e).__name__}), expected SafetyViolationError -> {e}")


def _expect_success(label: str, fn, *args, **kwargs) -> None:
    """Runs fn(*args, **kwargs) and asserts it does NOT raise."""
    try:
        result = fn(*args, **kwargs)
        print(f"[PASS] {label}: correctly allowed -> {result}")
    except SafetyViolationError as e:
        print(f"[FAIL] {label}: valid input was wrongly rejected -> {e}")


def main() -> None:
    print("=" * 70)
    print("LAYER 1 — run_safety_check() with malicious STRUCTURED DICTS")
    print("(tests whether the builder itself blocks bad structured input)")
    print("=" * 70)

    _expect_success(
        "sanity check: a normal, valid query",
        run_safety_check,
        {
            "table": "plans",
            "select_items": [{"column": "name", "aggregate": None, "alias": None}],
            "where_conditions": [],
            "combine_with": "AND",
            "group_by": [],
            "order_by": [],
        },
        valid_tables=VALID_TABLES,
        schema_context=FAKE_SCHEMA,
    )

    _expect_rejection(
        "table not in schema (attacker names a table that doesn't exist / isn't allowed)",
        run_safety_check,
        {
            "table": "admin_users",
            "select_items": [{"column": "*", "aggregate": None, "alias": None}],
            "where_conditions": [],
            "combine_with": "AND",
            "group_by": [],
            "order_by": [],
        },
        valid_tables=VALID_TABLES,
        schema_context=FAKE_SCHEMA,
    )

    _expect_rejection(
        "disallowed operator (attacker smuggles a non-whitelisted operator)",
        run_safety_check,
        {
            "table": "plans",
            "select_items": [{"column": "name", "aggregate": None, "alias": None}],
            "where_conditions": [
                {"column": "price", "operator": "; DROP TABLE plans; --", "value": 1}
            ],
            "combine_with": "AND",
            "group_by": [],
            "order_by": [],
        },
        valid_tables=VALID_TABLES,
        schema_context=FAKE_SCHEMA,
    )

    _expect_rejection(
        "malicious column name with SQL injection payload as the identifier itself",
        run_safety_check,
        {
            "table": "plans",
            "select_items": [
                {"column": "name; DROP TABLE plans; --", "aggregate": None, "alias": None}
            ],
            "where_conditions": [],
            "combine_with": "AND",
            "group_by": [],
            "order_by": [],
        },
        valid_tables=VALID_TABLES,
        schema_context=FAKE_SCHEMA,
    )

    _expect_rejection(
        "disallowed aggregate function (attacker tries a function not on the allowlist)",
        run_safety_check,
        {
            "table": "plans",
            "select_items": [
                {"column": "price", "aggregate": "SLEEP", "alias": None}
            ],
            "where_conditions": [],
            "combine_with": "AND",
            "group_by": [],
            "order_by": [],
        },
        valid_tables=VALID_TABLES,
        schema_context=FAKE_SCHEMA,
    )

    print()
    print("=" * 70)
    print("LAYER 2 — _ast_verify() with malicious SQL STRINGS, bypassing the")
    print("builder entirely. This proves the AST layer works independently,")
    print("as a defense against a hypothetical bug in the builder itself.")
    print("=" * 70)

    _expect_success(
        "sanity check: a normal, valid raw SELECT",
        _ast_verify,
        "SELECT `name` FROM `plans` LIMIT %s",
        FAKE_SCHEMA,
    )

    _expect_rejection(
        "raw DROP TABLE statement (simulates a builder bug that let a write through)",
        _ast_verify,
        "DROP TABLE `plans`",
        FAKE_SCHEMA,
    )

    _expect_rejection(
        "raw DELETE statement",
        _ast_verify,
        "DELETE FROM `plans` WHERE `id` = %s",
        FAKE_SCHEMA,
    )

    _expect_rejection(
        "raw UPDATE statement",
        _ast_verify,
        "UPDATE `plans` SET `price` = %s WHERE `id` = %s",
        FAKE_SCHEMA,
    )

    _expect_rejection(
        "stacked query — a SELECT followed by a second malicious statement",
        _ast_verify,
        "SELECT `name` FROM `plans`; DROP TABLE `plans`;",
        FAKE_SCHEMA,
    )

    _expect_rejection(
        "SELECT referencing a table not in the known schema",
        _ast_verify,
        "SELECT * FROM `admin_users`",
        FAKE_SCHEMA,
    )

    _expect_rejection(
        "SELECT referencing a column not in the known schema for that table",
        _ast_verify,
        "SELECT `password_hash` FROM `plans`",
        FAKE_SCHEMA,
    )

    _expect_rejection(
        "UNION-based injection attempt (a second SELECT smuggled via UNION)",
        _ast_verify,
        "SELECT `name` FROM `plans` UNION SELECT `password` FROM `admin_users`",
        FAKE_SCHEMA,
    )

    print()
    print("All safety_check.py checks completed.")


if __name__ == "__main__":
    main()