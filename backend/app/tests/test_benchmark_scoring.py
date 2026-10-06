"""Phase 2: honest benchmark scoring (offline, no LLM or DB)."""

from app.tests.advanced_query_generation_benchmark import _classify_outcome, _score
from app.tests.query_generation_benchmark import _case, _evaluate_plan


def _row(**kw):
    base = {
        "error": "", "error_bases": "", "error_cause": "",
        "rule_id": "", "error_stage": "", "plan_match": "not_run",
    }
    base.update(kw)
    return base


def test_grade_error_is_escalated_and_not_a_refusal():
    row = _row(error="GradeSampleError: x", error_bases="GradeSampleError,Exception")
    row["outcome"] = _classify_outcome(row)
    assert row["outcome"] == "escalated"
    assert _score({"expected_outcome": "refusal"}, row)[0] == "fail"


def test_grader_unavailable_is_not_evaluated():
    row = _row(error="SchemaGraderUnavailableError: x",
               error_bases="SchemaGraderUnavailableError,Exception")
    assert _classify_outcome(row) == "not_evaluated"


def test_unanswerable_is_a_confirmed_refusal():
    row = _row(error="UnanswerableQuestionError: x",
               error_bases="UnanswerableQuestionError,Exception")
    row["outcome"] = _classify_outcome(row)
    assert row["outcome"] == "confirmed_refusal"
    assert _score({"expected_outcome": "refusal"}, row)[0] == "pass"


def test_llm_failure_is_a_service_failure():
    row = _row(error="QueryGenerationError: x", error_bases="QueryGenerationError,Exception",
               error_cause="LLMServiceError")
    assert _classify_outcome(row) == "not_evaluated"


def test_safe_block_accepts_only_refusal_or_pre_sql_validation_rejection():
    case = {"expected_outcome": "safe_block"}
    refusal = _row(
        error="UnanswerableQuestionError: x",
        error_bases="UnanswerableQuestionError,Exception",
    )
    refusal["outcome"] = _classify_outcome(refusal)
    assert _score(case, refusal)[0] == "pass"

    rejected = _row(
        error="SafetyViolationError: x",
        error_bases="SafetyViolationError,Exception",
        safety_validation="failed",
    )
    rejected["outcome"] = _classify_outcome(rejected)
    assert _score(case, rejected)[0] == "pass"

    for failure in (
        _row(outcome="escalated"),
        _row(outcome="not_evaluated"),
        _row(
            outcome="validation_rejected",
            compiled_sql="SELECT 1",
            safety_validation="failed",
        ),
    ):
        assert _score(case, failure)[0] != "pass"


def test_safety_refusal_needs_the_exact_rule_and_stage():
    case = {"expected_outcome": "safety_refusal",
            "expected_rule_id": "fanout_having_aggregate",
            "expected_stage": "safety_check"}
    good = _row(error="SafetyViolationError: x", error_bases="SafetyViolationError,Exception",
                rule_id="fanout_having_aggregate", error_stage="safety_check")
    good["outcome"] = _classify_outcome(good)
    assert _score(case, good)[0] == "pass"

    wrong_rule = dict(good, rule_id="fanout_select_aggregate")
    assert _score(case, wrong_rule)[0] == "fail"
    wrong_stage = dict(good, error_stage="generator")
    assert _score(case, wrong_stage)[0] == "fail"


def test_sql_case_needs_answer_and_plan_match():
    row = _row(plan_match="yes")
    row["outcome"] = _classify_outcome(row)
    assert _score({"expected_outcome": "sql"}, row)[0] == "pass"
    row["plan_match"] = "no"
    assert _score({"expected_outcome": "sql"}, row)[0] == "fail"
def test_explain_rejection_is_its_own_outcome():
    row = _row(
        error="RuntimeError: MySQL EXPLAIN rejected generated SQL: (1055, 'x')",
        error_bases="RuntimeError,Exception",
    )
    assert _classify_outcome(row) == "sql_rejected_by_database"


def _anti_join_fixture(expected_null_column, actual_null_column="requester_id"):
    case = _case(
        1,
        "advanced",
        "Users without a booking",
        ["users", "bookings"],
        joins=[("bookings", "bookings_requester_user", "LEFT")],
        selects=[("users", "id", "COUNT", True)],
        filters=[("bookings", expected_null_column, "IS NULL", None)],
    )
    query = {
        "table": "users",
        "joins": [{
            "table": "bookings",
            "join_type": "LEFT",
            "relationship": {
                "constraint_name": "bookings_requester_user",
                "table": "bookings",
                "referenced_table": "users",
            },
        }],
        "select_items": [{
            "table": "users",
            "column": "id",
            "aggregate": "COUNT",
            "distinct": True,
            "alias": "user_count",
        }],
        "where_expression": {
            "table": "bookings",
            "column": actual_null_column,
            "operator": "IS NULL",
            "value": None,
        },
        "group_by": [],
        "having_expression": None,
        "count_entity": {"table": "users", "column": "id"},
        "order_by": [],
    }
    relationships = [{
        "constraint_name": "bookings_requester_user",
        "table": "bookings",
        "referenced_table": "users",
        "column_pairs": [{
            "column": "requester_id",
            "referenced_column": "id",
        }],
    }]
    schema = {
        "users": [{"column": "id", "key": "PRI"}],
        "bookings": [
            {"column": "id", "key": "PRI"},
            {"column": "requester_id", "key": ""},
            {"column": "created_at", "key": ""},
        ],
    }
    return case, query, relationships, schema


def test_anti_join_null_match_accepts_primary_key_to_verified_join_key():
    case, query, relationships, schema = _anti_join_fixture("id")
    assert not _evaluate_plan(case, query, relationships, schema)


def test_anti_join_null_match_accepts_verified_join_key_to_primary_key():
    case, query, relationships, schema = _anti_join_fixture(
        "requester_id", actual_null_column="id"
    )
    assert not _evaluate_plan(case, query, relationships, schema)


def test_anti_join_null_match_does_not_accept_other_nullable_columns():
    case, query, relationships, schema = _anti_join_fixture("id")
    query["where_expression"]["column"] = "created_at"
    failures = _evaluate_plan(case, query, relationships, schema)
    assert any("missing WHERE condition" in failure for failure in failures)


def test_inner_join_accepted_when_having_count_requires_a_match():
    case = {
        "expected_outcome": "sql",
        "expected_tables": ["shop_details", "offer"],
        "expected_joins": [
            {"table": "offer", "constraint_name": "c1", "join_type": "LEFT"}
        ],
        "expected_selects": [], "expected_filters": [], "expected_groups": [],
        "expected_having": [], "expected_order_by": [], "expected_limit": None,
        "expected_round_digits": None, "expected_row_listing": False,
        "expected_filter_boolean": None,
    }
    having = {"aggregate": "COUNT", "table": "offer", "column": "id",
              "operator": ">=", "value": 3}
    query = {
        "table": "shop_details",
        "joins": [{"table": "offer", "join_type": "INNER",
                   "relationship": {"constraint_name": "c1"}}],
        "select_items": [], "group_by": [], "having_expression": having,
    }
    failures = _evaluate_plan(case, query)
    assert not any("missing join" in f for f in failures)

    # zero-count (case 14 style) must still require LEFT
    query["having_expression"] = {**having, "operator": "=", "value": 0}
    assert any("missing join" in f for f in _evaluate_plan(case, query))