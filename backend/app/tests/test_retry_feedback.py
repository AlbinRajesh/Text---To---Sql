from app.agent.nodes.generate_query import QueryGenerationError, _retry_nudge_for


def test_retry_nudge_includes_the_exact_rejection_reason():
    error = QueryGenerationError(
        "COUNT in a multi-table query must count DISTINCT entity keys to prevent join fan-out.",
        rule_id="joined_count_not_distinct",
    )
    nudge = _retry_nudge_for(error)
    assert "must count DISTINCT entity keys" in nudge
    assert "set distinct to true" in nudge


def test_retry_nudge_without_a_known_rule_still_includes_the_reason():
    nudge = _retry_nudge_for(QueryGenerationError("something specific broke"))
    assert "something specific broke" in nudge