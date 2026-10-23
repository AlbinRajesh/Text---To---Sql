import pytest

from app.agent.nodes.generate_query import QueryGenerationError, _validate_llm_output


SCHEMA = {
    "shop_details": [
        {"column": "id", "key": "PRI"},
        {"column": "name"},
        {"column": "status"},
    ],
}


def _plan(where_expression=None):
    return {
        "answerable": True,
        "reasoning": "List matching shops.",
        "table": "shop_details",
        "joins": [],
        "select_items": [{
            "table": "shop_details",
            "column": "name",
            "aggregate": None,
            "alias": "shop_name",
        }],
        "where_expression": where_expression,
        "group_by": [],
        "having_expression": None,
        "count_groups": False,
        "count_entity": None,
        "order_by": [],
    }


def _status_filter(value):
    return {
        "table": "shop_details",
        "column": "status",
        "operator": "=",
        "value": value,
    }


def test_shop_queries_are_not_implicitly_filtered_to_active_status():
    normalized = _validate_llm_output(
        _plan(),
        SCHEMA,
        question="Find shops.",
    )

    assert normalized["where_expression"] is None


@pytest.mark.parametrize(
    ("state", "value"),
    [("active", 1), ("inactive", 0)],
)
def test_catalog_state_filter_is_required_when_explicitly_requested(state, value):
    state_filter = {
        "entity": "shop_details",
        "state": state,
        "business_rule_term": f"{state}_shop_status",
        "table": "shop_details",
        "column": "status",
        "value": value,
    }

    with pytest.raises(QueryGenerationError, match="catalog-defined current-state"):
        _validate_llm_output(
            _plan(),
            SCHEMA,
            question=f"Find {state} shops.",
            current_state_filters=[state_filter],
        )

    normalized = _validate_llm_output(
        _plan(_status_filter(value)),
        SCHEMA,
        question=f"Find {state} shops.",
        current_state_filters=[state_filter],
    )
    assert normalized["where_expression"] == _status_filter(value)
