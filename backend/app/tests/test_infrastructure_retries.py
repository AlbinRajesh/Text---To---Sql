import asyncio

import aiomysql
import pytest

from app.agent.nodes import run_self_fix
from app.agent.nodes.run_self_fix import (
    MAX_QUERY_REPAIR_ATTEMPTS,
    QueryExecutionError,
    QueryInfrastructureError,
    QueryRepairError,
    _fetch_with_retries,
    run_query_with_repair,
)


def test_query_repair_attempt_limit_cannot_exceed_three():
    async def unused_repair(error, attempt):
        raise AssertionError("invalid attempt limits must be rejected first")

    with pytest.raises(ValueError, match="between 1 and 3"):
        asyncio.run(run_query_with_repair(
            {"sql": "SELECT 1"},
            unused_repair,
            max_attempts=MAX_QUERY_REPAIR_ATTEMPTS + 1,
        ))


def test_database_connection_failure_retries_with_exponential_backoff(monkeypatch):
    calls = []
    sleeps = []

    async def failed_fetch(sql, params):
        calls.append((sql, params))
        raise aiomysql.OperationalError(2013, "connection lost")

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(run_self_fix, "fetch_all_readonly", failed_fetch)
    monkeypatch.setattr(run_self_fix.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(run_self_fix, "MAX_EXECUTION_RETRIES", 3)
    monkeypatch.setattr(run_self_fix, "EXECUTION_BASE_BACKOFF_SECONDS", 0.1)

    with pytest.raises(QueryInfrastructureError) as error:
        asyncio.run(_fetch_with_retries("SELECT 1", (), "Query"))

    assert error.value.infrastructure_code == "DB_CONNECTION_ERROR"
    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]


def test_query_execution_timeout_is_retried_and_classified_as_infrastructure(monkeypatch):
    calls = []
    sleeps = []

    async def timed_out_query(sql, params):
        calls.append((sql, params))
        raise aiomysql.OperationalError(3024, "execution timeout")

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(run_self_fix, "fetch_all_readonly", timed_out_query)
    monkeypatch.setattr(run_self_fix.asyncio, "sleep", fake_sleep)

    with pytest.raises(QueryInfrastructureError, match="DB_QUERY_TIMEOUT") as error:
        asyncio.run(_fetch_with_retries("SELECT 1", (), "Query"))

    assert error.value.infrastructure_code == "DB_QUERY_TIMEOUT"
    assert len(calls) == 3
    assert sleeps == [0.25, 0.5]


def test_query_shape_error_is_repaired_and_reexecuted(monkeypatch):
    execution_calls = []
    repair_errors = []

    async def fake_run_query(query):
        execution_calls.append(query)
        if query["sql"] == "bad":
            raise QueryExecutionError(
                "Query failed: Unknown column 'missing' in 'field list'",
                mysql_code=1054,
            )
        return {"results": [{"count": 1}]}

    async def fake_repair(error, attempt):
        repair_errors.append((str(error), attempt))
        return {"sql": "repaired"}

    monkeypatch.setattr(run_self_fix, "run_query", fake_run_query)

    result = asyncio.run(run_query_with_repair(
        {"sql": "bad"},
        fake_repair,
    ))

    assert result == {"results": [{"count": 1}]}
    assert execution_calls == [{"sql": "bad"}, {"sql": "repaired"}]
    assert repair_errors == [
        ("Query failed: Unknown column 'missing' in 'field list'", 1)
    ]


def test_invalid_repair_attempts_are_bounded_without_reexecuting_original(
    monkeypatch,
):
    execution_calls = []
    repair_calls = []

    async def fake_run_query(query):
        execution_calls.append(query)
        raise QueryExecutionError("Unknown column", mysql_code=1054)

    async def invalid_repair(error, attempt):
        repair_calls.append((str(error), attempt))
        raise QueryRepairError(f"Validation failed: retry {attempt}")

    monkeypatch.setattr(run_self_fix, "run_query", fake_run_query)

    with pytest.raises(QueryExecutionError, match="exhausted 3 total attempts"):
        asyncio.run(run_query_with_repair(
            {"sql": "initial"},
            invalid_repair,
        ))

    assert execution_calls == [{"sql": "initial"}]
    assert len(repair_calls) == 2
    assert "retry 1" in repair_calls[1][0]


def test_infrastructure_errors_never_enter_model_repair(monkeypatch):
    repair_calls = []

    async def unavailable_query(query):
        raise QueryInfrastructureError("DB timeout", "DB_QUERY_TIMEOUT")

    async def unexpected_repair(error, attempt):
        repair_calls.append((error, attempt))
        raise AssertionError("infrastructure errors must not trigger model repair")

    monkeypatch.setattr(run_self_fix, "run_query", unavailable_query)

    with pytest.raises(QueryInfrastructureError):
        asyncio.run(run_query_with_repair(
            {"sql": "SELECT 1"},
            unexpected_repair,
        ))

    assert repair_calls == []
