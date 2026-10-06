from app.agent.nodes.generate_query import (
    _RETRY_NUDGE,
    QueryGenerationError,
    _retry_nudge_for,
)


def test_count_primary_key_rejection_gets_specific_hint():
    error = QueryGenerationError("x", rule_id="joined_count_not_primary_key")
    nudge = _retry_nudge_for(error)
    assert nudge.startswith(_RETRY_NUDGE)
    assert "must appear in joins" in nudge


def test_other_rejections_keep_the_generic_nudge():
    assert _retry_nudge_for(QueryGenerationError("x")) == _RETRY_NUDGE


def test_joined_numeric_aggregate_rejection_gets_base_table_hint():
    error = QueryGenerationError("x", rule_id="fanout_select_aggregate")
    nudge = _retry_nudge_for(error)
    assert nudge.startswith(_RETRY_NUDGE)
    assert "table that contains the measured column the base table" in nudge