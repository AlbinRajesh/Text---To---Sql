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
    nudge = _retry_nudge_for(QueryGenerationError("x"))
    assert nudge.startswith(_RETRY_NUDGE)
    assert "Your previous plan was rejected because: x" in nudge


def test_joined_numeric_aggregate_rejection_gets_base_table_hint():
    error = QueryGenerationError("x", rule_id="fanout_select_aggregate")
    nudge = _retry_nudge_for(error)
    assert nudge.startswith(_RETRY_NUDGE)
    assert "table that contains the measured column the base table" in nudge

def test_fanout_aggregate_with_exclusions_gets_multi_child_hint():
    error = QueryGenerationError("x", rule_id="fanout_select_aggregate")
    parsed = {
        "table": "shop_details",
        "exclude_entities_with_rows_in": [{"table": "bookings"}],
    }
    nudge = _retry_nudge_for(error, parsed=parsed)
    assert "Keep the primary entity table as the base table" in nudge
    assert "exclude_entities_with_rows_in for anti-joins" in nudge


def test_fanout_aggregate_with_multiple_joins_gets_multi_child_hint():
    error = QueryGenerationError("x", rule_id="fanout_select_aggregate")
    parsed = {
        "table": "shop_details",
        "joins": [{"table": "reviews"}, {"table": "bookings"}],
    }
    nudge = _retry_nudge_for(error, parsed=parsed)
    assert "Keep the primary entity table as the base table" in nudge
    assert "isolated_child_aggregates for child metrics" in nudge


def test_fanout_aggregate_with_anti_join_filter_gets_multi_child_hint():
    error = QueryGenerationError("x", rule_id="fanout_select_aggregate")
    parsed = {
        "table": "shop_details",
        "joins": [{"table": "bookings"}],
        "where_expression": {"table": "bookings", "column": "id", "operator": "IS NULL", "value": None},
    }
    nudge = _retry_nudge_for(error, parsed=parsed)
    assert "Keep the primary entity table as the base table" in nudge
    assert "exclude_entities_with_rows_in for anti-joins" in nudge


def test_fanout_aggregate_pure_single_metric_retains_base_inversion_hint():
    error = QueryGenerationError("x", rule_id="fanout_select_aggregate")
    parsed = {
        "table": "shop_details",
        "joins": [{"table": "reviews"}],
        "where_expression": None,
    }
    nudge = _retry_nudge_for(error, parsed=parsed)
    assert "table that contains the measured column the base table" in nudge
