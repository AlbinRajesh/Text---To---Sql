"""Phase 3: typed 'missing relationship' route (offline, no LLM or DB)."""

import asyncio

import pytest

from app.agent.nodes import grade_sample
from app.agent.nodes.grade_sample import (
    GradeSampleError,
    MissingRelationshipError,
    SchemaGraderUnavailableError,
    grade_schema_sufficiency,
)
from app.tests.advanced_query_generation_benchmark import _classify_outcome

SCHEMA = {name: [{"column": "id"}] for name in ("a", "b", "c", "d", "e")}
# a - b - c are connected (a to c needs two hops); d and e are isolated.
RELS = [
    {"constraint_name": "a_b", "table": "a", "referenced_table": "b"},
    {"constraint_name": "b_c", "table": "b", "referenced_table": "c"},
]


def _install(monkeypatch, verdicts):
    calls = {"retrieve": 0, "grade": 0}

    async def fake_retrieve(question, top_k=6):
        calls["retrieve"] += 1
        return list(SCHEMA)

    async def fake_grade(original_question, rewritten_question, schema_text, relationships):
        outcome = verdicts[min(calls["grade"], len(verdicts) - 1)]
        calls["grade"] += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(grade_sample, "get_cached_schema", lambda: SCHEMA)
    monkeypatch.setattr(grade_sample, "get_cached_relationships", lambda: RELS)
    monkeypatch.setattr(grade_sample, "retrieve_relevant_tables", fake_retrieve)
    monkeypatch.setattr(grade_sample, "_ask_cheap_model_if_sufficient", fake_grade)
    return calls


def _run():
    return asyncio.run(grade_schema_sufficiency("q"))


def _rel(*tables, kind="relationship"):
    return (False, [{"type": kind, "detail": "needs join", "tables": list(tables)}])


def test_disconnected_tables_refuse_on_first_attempt(monkeypatch):
    calls = _install(monkeypatch, [_rel("a", "d")])
    with pytest.raises(MissingRelationshipError) as info:
        _run()
    assert info.value.tables == ("a", "d")
    assert calls["grade"] == 1 and calls["retrieve"] == 1
        # user-facing text must not leak the grader's detail or internals
    assert "needs join" not in info.value.reasoning
    assert "No verified relationship path" not in info.value.reasoning


def test_missing_relationship_error_is_not_a_grade_sample_error():
    assert not issubclass(MissingRelationshipError, GradeSampleError)


def test_multi_hop_path_in_catalog_keeps_widening(monkeypatch):
    calls = _install(monkeypatch, [_rel("a", "c")])
    with pytest.raises(GradeSampleError):
        _run()
    assert calls["grade"] == 3


def test_missing_column_keeps_widening(monkeypatch):
    calls = _install(monkeypatch, [_rel("a", "d", kind="column")])
    with pytest.raises(GradeSampleError):
        _run()
    assert calls["grade"] == 3


def test_plain_string_items_still_work_and_widen(monkeypatch):
    calls = _install(monkeypatch, [(False, ["bookings.user_id"])])
    with pytest.raises(GradeSampleError) as info:
        _run()
    assert calls["grade"] == 3
    assert "bookings.user_id" in str(info.value)


def test_unknown_type_never_triggers_early_exit(monkeypatch):
    calls = _install(monkeypatch, [_rel("a", "d", kind="whatever")])
    with pytest.raises(GradeSampleError):
        _run()
    assert calls["grade"] == 3


def test_nonexistent_table_never_triggers_early_exit(monkeypatch):
    calls = _install(monkeypatch, [_rel("a", "ghost_table")])
    with pytest.raises(GradeSampleError):
        _run()
    assert calls["grade"] == 3


def test_relationship_needs_exactly_two_distinct_tables(monkeypatch):
    for tables in (("a",), ("d", "d"), ("a", "b", "d")):
        calls = _install(monkeypatch, [_rel(*tables)])
        with pytest.raises(GradeSampleError):
            _run()
        assert calls["grade"] == 3


def test_grader_outage_still_propagates(monkeypatch):
    _install(monkeypatch, [SchemaGraderUnavailableError("down")])
    with pytest.raises(SchemaGraderUnavailableError):
        _run()


def test_sufficient_schema_returns_normally(monkeypatch):
    _install(monkeypatch, [(True, [])])
    result = _run()
    assert result["sufficient"] is True and result["attempts"] == 1


def test_graph_node_sets_refusal_and_routes_to_return_save(monkeypatch):
    from app.agent import graph

    async def fake_grade(question, original_question=None):
        raise MissingRelationshipError(("a", "d"), "needs join")

    monkeypatch.setattr(graph, "grade_schema_sufficiency", fake_grade)
    state = {"classifier_result": {"rewritten_question": "q"}}
    result = asyncio.run(graph.node_grade_schema(state))
    assert result["answerable"] is False
    assert result["unanswerable_reasoning"]
    assert result["grade_sample_exhausted"] is False
    assert not result.get("escalated")
    assert graph.route_after_grade_schema(result) == "return_save"


def test_route_after_grade_schema_other_paths():
    from app.agent import graph

    assert graph.route_after_grade_schema({"grade_sample_exhausted": True}) == "escalation"
    assert graph.route_after_grade_schema({"grade_sample_exhausted": False}) == "business_rules"
    # answerable=False without a reasoning must not divert the normal path
    assert graph.route_after_grade_schema({"answerable": False}) == "business_rules"


def test_benchmark_counts_missing_relationship_as_confirmed_refusal():
    row = {
        "error": "MissingRelationshipError: x",
        "error_bases": "MissingRelationshipError,Exception",
        "error_cause": "",
    }
    assert _classify_outcome(row) == "confirmed_refusal"
