import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.agent.nodes import classifier_rewrite
from app.agent.nodes.return_save import build_and_save_answer
from app.agent.prompts.classifier_prompt import SYSTEM_PROMPT
from app.agent import catalog_resolver
from app.agent import graph
from app.agent.state import new_state
from app.api import chat
from app.services import pending_interactions as pending_store


@pytest.fixture(autouse=True)
def memory_pending_store(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(
        pending_store,
        "get_suggestion_redis_client",
        lambda: None,
    )
    asyncio.run(pending_store.initialize_pending_interaction_store())
    pending_store.clear_all_pending_interactions()
    yield
    asyncio.run(pending_store.close_pending_interaction_store())


def _store_pending_interaction(*args, **kwargs):
    return asyncio.run(chat.store_pending_interaction(*args, **kwargs))


def _get_pending_interaction(*args, **kwargs):
    return asyncio.run(chat.get_pending_interaction(*args, **kwargs))


def _store_clarification_if_needed(*args, **kwargs):
    return asyncio.run(chat._store_clarification_if_needed(*args, **kwargs))


def test_pasted_select_requires_confirmation_then_executes_once(monkeypatch):
    monkeypatch.setattr(chat, "get_cached_schema", lambda: {"shops": []})
    monkeypatch.setattr(
        chat,
        "validate_user_select",
        lambda _sql, _schema: "SELECT name FROM shops LIMIT 201",
    )
    staged, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "SELECT name FROM shops",
            "request-1",
            "conversation-1",
        )
    )
    assert staged.status == "confirmation"
    assert staged.confirmation_sql == "SELECT name FROM shops LIMIT 201"

    executed = []

    async def fake_execute(sql, _schema):
        executed.append(sql)
        return {
            "results": [{"name": "Sample shop"}],
            "columns": ["name"],
            "count": 1,
            "row_count": 1,
            "truncated": False,
            "row_limit": 200,
        }

    monkeypatch.setattr(chat, "execute_user_select", fake_execute)
    result, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "yes",
            "request-2",
            "conversation-1",
        )
    )
    assert result.status == "ok"
    assert result.results == [{"name": "Sample shop"}]
    assert executed == ["SELECT name FROM shops LIMIT 201"]

    second_result, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "yes",
            "request-3",
            "conversation-1",
        )
    )
    assert second_result is None
    assert len(executed) == 1


def test_short_reply_without_an_option_is_invalid():
    _store_pending_interaction(
        "conversation-2",
        "clarification",
        {
            "question": "Which category generated booking revenue?",
            "clarification_question": "Which category type?",
            "options": [],
        },
    )

    direct_response, continued_question = asyncio.run(
        chat._handle_pending_or_sql(
            "plan",
            "request-4",
            "conversation-2",
        )
    )

    assert direct_response.status == "clarification"
    assert direct_response.message == "Which category type?"
    assert continued_question == "plan"
    assert _get_pending_interaction("conversation-2")["payload"][
        "invalid_replies"
    ] == 1


def test_yes_does_not_loop_an_either_or_clarification_through_the_classifier():
    _store_pending_interaction(
        "conversation-clarification",
        "clarification",
        {
            "question": "Which subcategories have the most active shops?",
            "clarification_question": "Do you want subcategory names or shop counts?",
            "options": [],
        },
    )

    response, routed_question = asyncio.run(
        chat._handle_pending_or_sql(
            "yes",
            "request-clarification",
            "conversation-clarification",
        )
    )

    assert response.status == "clarification"
    assert response.message == "Do you want subcategory names or shop counts?"
    assert routed_question == "yes"
    assert _get_pending_interaction("conversation-clarification")["payload"][
        "invalid_replies"
    ] == 1


def test_yes_to_choose_one_reprompts_once_then_closes_note():
    _store_pending_interaction(
        "conversation-yes-choice",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "unclear_phrase": "juice",
            "options": [
                {"label": "Drinks & Beverages", "value": "Drinks & Beverages", "level": "subcategory"},
            ],
            "invalid_replies": 0,
            "asked_items": ["juice"],
            "clarification_question": "Which subcategory do you mean by 'juice'?",
        },
    )

    first_response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "yes", "request-yes-1", "conversation-yes-choice"
        )
    )
    assert first_response.status == "clarification"
    assert first_response.clarification_options == ["Drinks & Beverages"]
    assert routed == "yes"
    assert _get_pending_interaction("conversation-yes-choice")["payload"][
        "invalid_replies"
    ] == 1

    second_response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "yes", "request-yes-2", "conversation-yes-choice"
        )
    )
    assert second_response.status == "chat"
    assert "couldn't resolve" in second_response.message
    assert routed == "yes"
    assert _get_pending_interaction("conversation-yes-choice") is None


def test_long_exact_catalog_option_is_accepted():
    _store_pending_interaction(
        "conversation-long-option",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [
                {
                    "label": "Drinks & Beverages",
                    "value": "Drinks & Beverages",
                    "level": "subcategory",
                }
            ],
            "invalid_replies": 0,
            "clarification_question": "Which subcategory do you mean?",
        },
    )

    response, continued = asyncio.run(
        chat._handle_pending_or_sql(
            "Drinks & Beverages",
            "request-long-option",
            "conversation-long-option",
        )
    )

    assert response is None
    assert "The user selected: Drinks & Beverages." in continued


def test_exact_option_accepts_case_spacing_and_trailing_punctuation():
    _store_pending_interaction(
        "conversation-exact-option",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [{"label": "Drinks & Beverages"}],
            "clarification_question": "Which category do you mean?",
        },
    )

    response, continued = asyncio.run(
        chat._handle_pending_or_sql(
            "  drinks   & beverages. ",
            "request-exact-option",
            "conversation-exact-option",
        )
    )

    assert response is None
    assert "The user selected: Drinks & Beverages." in continued


def test_option_prefix_is_invalid_in_phase_3a():
    _store_pending_interaction(
        "conversation-option-prefix",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [{"label": "Drinks & Beverages"}],
            "clarification_question": "Which subcategory do you mean?",
        },
    )

    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "drinks",
            "request-option-prefix",
            "conversation-option-prefix",
        )
    )

    assert response.status == "clarification"
    assert response.clarification_options == ["Drinks & Beverages"]
    assert _get_pending_interaction("conversation-option-prefix")[
        "payload"
    ]["invalid_replies"] == 1


def test_clear_typo_selects_the_best_clarification_option():
    options = [
        {"label": "Restaurent"},
        {"label": "Real estate"},
    ]
    _store_pending_interaction(
        "conversation-clear-typo",
        "clarification",
        {
            "original_question": "Show restaurants",
            "kind": "choose-one",
            "options": options,
            "clarification_question": "Which subcategory do you mean?",
        },
    )

    response, continued = asyncio.run(
        chat._handle_pending_or_sql(
            "restaruent",
            "request-clear-typo",
            "conversation-clear-typo",
        )
    )

    assert response is None
    assert "The user selected: Restaurent." in continued


def test_selected_option_reaches_query_generation_as_catalog_filter(monkeypatch):
    filters = [
        {
            "level": "subcategory",
            "value": "Drinks & Beverages",
            "exclude": False,
        }
    ]
    monkeypatch.setattr(
        catalog_resolver,
        "resolve_business_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolver must not re-resolve a selected option")
        ),
    )

    state = new_state(
        "Show juice shops in Nagercoil",
        "request-selected-filter",
        catalog_filters=filters,
        cache_question=(
            "Show juice shops in Nagercoil\n"
            "Clarification: The user selected: Drinks & Beverages."
        ),
    )
    asyncio.run(graph.node_classify(state))
    assert state["classifier_result"]["catalog_filters"] == filters
    assert state["classifier_result"]["cache_question"].endswith(
        "Clarification: The user selected: Drinks & Beverages."
    )

    generated_options = {}

    async def fake_generate_query(
        *_args,
        **kwargs,
    ):
        generated_options.update(kwargs)
        return {}

    monkeypatch.setattr(graph, "generate_query", fake_generate_query)
    monkeypatch.setattr(
        graph,
        "validate_catalog_filter_plan",
        lambda *_args, **_kwargs: None,
    )
    state["schema_context"] = {}
    state["matched_business_rules"] = []
    state["schema_relationships"] = []
    asyncio.run(graph.node_generate_query(state))

    assert generated_options["catalog_filters"] == filters


def test_selected_option_without_level_uses_text_fallback():
    conversation_id = "conversation-option-no-level"
    _store_pending_interaction(
        conversation_id,
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "clarification_question": "Which subcategory do you mean?",
            "options": [
                {"label": "Drinks & Beverages", "value": "Drinks & Beverages"}
            ],
        },
    )
    pending = _get_pending_interaction(conversation_id)
    reply = "Drinks & Beverages"
    response, pipeline_question = asyncio.run(
        chat._handle_pending_or_sql(
            reply,
            "request-option-no-level",
            conversation_id,
        )
    )

    assert response is None
    assert chat._resumed_catalog_filters(
        pending,
        reply,
        pipeline_question,
    ) == []
    assert "The user selected: Drinks & Beverages." in pipeline_question


def test_exact_and_typo_replies_build_the_same_labeled_filter():
    pending = {
        "type": "clarification",
        "payload": {
            "options": [
                {
                    "label": "Drinks & Beverages",
                    "value": "Drinks & Beverages",
                    "level": "subcategory",
                },
                {
                    "label": "Snacks & Beverages",
                    "value": "Snacks & Beverages",
                    "level": "subcategory",
                },
            ]
        },
    }
    pipeline_question = (
        "Show juice shops\n"
        "Clarification: The user selected: Drinks & Beverages."
    )

    exact_filters = chat._resumed_catalog_filters(
        pending,
        "Drinks & Beverages",
        pipeline_question,
    )
    typo_filters = chat._resumed_catalog_filters(
        pending,
        "driks & beverages",
        pipeline_question,
    )

    assert exact_filters == [
        {
            "level": "subcategory",
            "value": "Drinks & Beverages",
            "exclude": False,
        }
    ]
    assert typo_filters == exact_filters


def test_widening_note_is_shown_when_selected_option_is_broader():
    pending = {
        "type": "clarification",
        "payload": {
            "unclear_phrase": "juice",
            "options": [{
                "label": "Drinks & Beverages",
                "value": "Drinks & Beverages",
                "level": "subcategory",
            }],
        },
    }
    pipeline_question = (
        "Show juice shops\n"
        "Clarification: The user selected: Drinks & Beverages."
    )
    unclear_phrase, chosen_value = chat._resumed_widening_context(
        pending, "Drinks & Beverages", pipeline_question
    )
    state = new_state(
        "Show juice shops",
        "request-widening",
        widening_unclear_phrase=unclear_phrase,
        widening_chosen_value=chosen_value,
    )
    state["final_answer"] = {
        "results": [{"name": "Sample shop"}],
        "columns": ["name"],
        "count": 1,
        "answer_type": "table",
    }

    response = chat._build_response(state, "request-widening")

    assert response.message == (
        "Showing shops classified under Drinks & Beverages. "
        "This doesn't show which shops sell juice."
    )


def test_name_search_option_is_stored_and_recovers_name_filter(monkeypatch):
    monkeypatch.setattr(chat, "get_business_value_catalog", lambda: [])
    state = {
        "user_question": "Show juice shops",
        "classifier_result": {},
        "final_answer": {
            "clarification_question": "I couldn't find a shop type like 'juice'.",
            "clarification_options": ["Search shop names for 'juice'"],
            "clarification_option_records": [{
                "label": "Search shop names for 'juice'",
                "kind": "name_search",
                "term": "juice",
            }],
            "clarification_kind": "choose_one",
        },
    }
    _store_clarification_if_needed(state, "name-search-conversation")
    pending = _get_pending_interaction("name-search-conversation")
    assert pending["payload"]["options"] == [{
        "label": "Search shop names for 'juice'",
        "kind": "name_search",
        "term": "juice",
    }]

    selected = "Search shop names for 'juice'"
    pipeline_question = (
        "Show juice shops\n"
        "Clarification: The user selected: Search shop names for 'juice'."
    )
    assert chat._resumed_catalog_filters(
        pending, selected, pipeline_question
    ) == [{"kind": "name_search", "term": "juice"}]
    assert chat._resumed_widening_context(
        pending, selected, pipeline_question
    ) == (None, None)


def test_name_search_note_is_added_only_to_response_and_handles_no_rows():
    state = {
        "classifier_result": {
            "catalog_filters": [{"kind": "name_search", "term": "juice"}],
        },
        "final_answer": {
            "results": [{"name": "A1 Juice house"}],
            "columns": ["name"],
            "count": 1,
            "answer_type": "table",
        },
    }
    response = chat._build_response(state, "request-name-search")
    assert response.message == (
        "Showing shops with 'juice' in their name. "
        "This doesn't show which shops sell juice."
    )
    assert "message" not in state["final_answer"]

    state["final_answer"]["results"] = []
    state["final_answer"]["count"] = 0
    empty_response = chat._build_response(state, "request-name-search-empty")
    assert empty_response.message == "No shops found with 'juice' in their name."

    state["generated_query"] = {
        "joins": [{
            "table": "shop_address",
            "conditions": [{
                "table": "shop_address",
                "column": "city",
                "operator": "=",
                "value": "Nagercoil",
            }],
        }],
    }
    city_response = chat._build_response(state, "request-name-search-city")
    assert city_response.message == (
        "No shops found with 'juice' in their name in Nagercoil."
    )


def test_repeated_name_search_selection_uses_stale_clarification_path(monkeypatch):
    pending = {
        "type": "clarification",
        "interaction_id": "already-consumed",
        "version": 1,
        "payload": {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [{
                "label": "Search shop names for 'juice'",
                "kind": "name_search",
                "term": "juice",
            }],
        },
    }

    async def get_pending(_conversation_id):
        return pending

    async def already_consumed(*_args):
        return False

    monkeypatch.setattr(chat, "get_pending_interaction", get_pending)
    monkeypatch.setattr(
        chat,
        "consume_pending_interaction_if_matches",
        already_consumed,
    )
    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "Search shop names for 'juice'",
            "request-stale-name-search",
            "conversation-stale-name-search",
        )
    )
    assert response.status == "chat"
    assert "already handled or has expired" in response.message


def test_widening_context_is_forwarded_through_chat_and_graph(
    monkeypatch, tmp_path
):
    captured_options = {}
    captured_state = {}

    monkeypatch.setenv("TEXT_TO_SQL_TRACE_DIR", str(tmp_path / "pipeline_traces"))

    async def fake_ainvoke(_self, state, config=None):
        captured_state.update(state)
        captured_state["graph_config"] = config
        return state

    class FakeGraph:
        ainvoke = fake_ainvoke

    monkeypatch.setattr(graph, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(graph, "get_conversation_context", lambda _id: [])
    monkeypatch.setattr(graph, "start_request", lambda: [])

    async def fake_run_pipeline(question, **kwargs):
        captured_options.update(kwargs)
        return await graph.run_pipeline(question, **kwargs)

    monkeypatch.setattr(chat, "run_pipeline", fake_run_pipeline)
    state = asyncio.run(
        chat._run_chat_pipeline(
            "Show juice shops",
            "request-widening-plumbing",
            "conversation-widening-plumbing",
            widening_unclear_phrase="juice",
            widening_chosen_value="Drinks & Beverages",
        )
    )

    assert captured_options["widening_unclear_phrase"] == "juice"
    assert captured_options["widening_chosen_value"] == "Drinks & Beverages"
    assert captured_state["widening_unclear_phrase"] == "juice"
    assert captured_state["widening_chosen_value"] == "Drinks & Beverages"
    assert state["widening_unclear_phrase"] == "juice"


def test_widening_note_is_skipped_when_phrase_is_in_chosen_value():
    state = {
        "final_answer": {"results": [], "columns": [], "count": 0},
        "widening_unclear_phrase": "beverages",
        "widening_chosen_value": "Drinks & Beverages",
    }

    response = chat._build_response(state, "request-no-widening")

    assert response.message is None


def test_cache_hit_widening_note_does_not_mutate_cached_answer():
    cached_answer = {
        "results": [{"name": "Sample shop"}],
        "columns": ["name"],
        "count": 1,
        "answer_type": "table",
        "message": "Cached result.",
    }
    original_cached_answer = dict(cached_answer)

    response = chat._build_response(
        {
            "final_answer": cached_answer,
            "cache_hit": True,
            "widening_unclear_phrase": "juice",
            "widening_chosen_value": "Drinks & Beverages",
        },
        "request-cache-widening",
    )

    assert response.message == (
        "Cached result. Showing shops classified under Drinks & Beverages. "
        "This doesn't show which shops sell juice."
    )
    assert cached_answer == original_cached_answer


def test_partial_answer_note_is_added_once_at_response_time():
    response = chat._build_response(
        {
            "final_answer": {
                "results": [{"shop_name": "Sample shop"}],
                "columns": ["shop_name"],
                "count": 1,
                "answer_type": "table",
                "message": "Returned 1 row.",
            },
            "classifier_result": {
                "unresolved_catalog_items": ["spaceships"],
            },
        },
        "request-partial",
    )

    assert response.message == (
        "Returned 1 row. I couldn't find data for: spaceships."
    )
    assert response.message.count("I couldn't find data for:") == 1


def test_zero_row_partial_message_names_resolved_filter_and_unresolved_item():
    response = chat._build_response(
        {
            "final_answer": {
                "results": [],
                "columns": ["shop_id", "shop_name"],
                "count": 0,
                "row_count": 0,
                "answer_type": "empty",
                "message": "No data found for this request.",
            },
            "classifier_result": {
                "catalog_filters": [{
                    "level": "subcategory",
                    "value": "Snacks & Beverages",
                    "exclude": False,
                }],
                "unresolved_catalog_items": ["spaceships"],
            },
        },
        "request-partial-empty",
    )

    assert response.message == (
        "No shops found for Snacks & Beverages. "
        "I couldn't find data for: spaceships."
    )


def test_non_partial_zero_row_message_is_unchanged():
    response = chat._build_response(
        {
            "final_answer": {
                "results": [],
                "columns": ["shop_name"],
                "count": 0,
                "row_count": 0,
                "answer_type": "empty",
                "message": "No data found for this request.",
            },
            "classifier_result": {},
        },
        "request-empty",
    )

    assert response.message == "No data found for this request."


def test_partial_and_widening_notes_are_both_added_at_response_time():
    response = chat._build_response(
        {
            "final_answer": {
                "results": [{"shop_name": "Sample shop"}],
                "columns": ["shop_name"],
                "count": 1,
                "answer_type": "table",
            },
            "classifier_result": {
                "unresolved_catalog_items": ["spaceships", "unicorns"],
            },
            "widening_unclear_phrase": "juice",
            "widening_chosen_value": "Drinks & Beverages",
        },
        "request-partial-widening",
    )

    assert response.message == (
        "Showing shops classified under Drinks & Beverages. "
        "This doesn't show which shops sell juice. "
        "I couldn't find data for: spaceships, unicorns."
    )


def test_partial_request_skips_cache_lookup(monkeypatch):
    async def unexpected_cache_lookup(*_args, **_kwargs):
        raise AssertionError("Partial requests must bypass cache lookup.")

    monkeypatch.setattr(graph, "_current_context_version", lambda: "version")
    monkeypatch.setattr(graph, "check_cache", unexpected_cache_lookup)
    state = asyncio.run(graph.node_cache_check({
        "classifier_result": {
            "rewritten_question": "Show shops that sell snacks or spaceships",
            "catalog_filters": [{
                "level": "subcategory",
                "value": "Snacks & Beverages",
                "exclude": False,
            }],
            "catalog_filter_combiner": "OR",
            "unresolved_catalog_items": ["spaceships"],
        },
    }))

    assert state["cache_hit"] is False
    assert state["cached_answer"] is None


def test_no_clarification_has_no_widening_note():
    response = chat._build_response(
        {
            "final_answer": {
                "results": [{"name": "Sample shop"}],
                "columns": ["name"],
                "count": 1,
                "answer_type": "table",
            },
        },
        "request-no-clarification",
    )

    assert response.message is None


def test_note_without_unclear_phrase_has_no_widening_note():
    pending = {
        "type": "clarification",
        "payload": {
            "options": [{
                "label": "Drinks & Beverages",
                "value": "Drinks & Beverages",
                "level": "subcategory",
            }],
        },
    }
    pipeline_question = (
        "Show shops\n"
        "Clarification: The user selected: Drinks & Beverages."
    )
    unclear_phrase, chosen_value = chat._resumed_widening_context(
        pending, "Drinks & Beverages", pipeline_question
    )
    response = chat._build_response(
        {
            "final_answer": {"results": [], "columns": [], "count": 0},
            "widening_unclear_phrase": unclear_phrase,
            "widening_chosen_value": chosen_value,
        },
        "request-no-unclear-phrase",
    )

    assert response.message is None


def test_close_typo_score_tie_is_rejected_by_margin():
    options = [
        {"label": "Drinks & Beverages"},
        {"label": "Drinks and Beverages"},
    ]
    assert chat._fuzzy_clarification_option("drinks beverags", options) is None
    _store_pending_interaction(
        "conversation-typo-tie",
        "clarification",
        {
            "original_question": "Show drinks shops",
            "kind": "choose-one",
            "options": options,
            "clarification_question": "Which drink type do you mean?",
        },
    )

    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "drinks beverags",
            "request-typo-tie",
            "conversation-typo-tie",
        )
    )

    assert response.status == "clarification"
    assert _get_pending_interaction("conversation-typo-tie")["payload"][
        "invalid_replies"
    ] == 1


def test_banana_does_not_match_clarification_options():
    options = [
        {"label": "Drinks & Beverages"},
        {"label": "Snacks & Beverages"},
    ]
    assert chat._fuzzy_clarification_option("banana", options) is None


def test_option_prefix_is_scored_not_prefix_matched():
    options = [
        {"label": "Drinks & Beverages"},
        {"label": "Train"},
    ]
    assert chat._matched_clarification_option("drinks", options) is None
    assert chat._fuzzy_clarification_option("drinks", options) is None
    _store_pending_interaction(
        "conversation-prefix-scorer",
        "clarification",
        {
            "original_question": "Show drinks shops",
            "kind": "choose-one",
            "options": options,
            "clarification_question": "Which subcategory do you mean?",
        },
    )

    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "drinks",
            "request-prefix-scorer",
            "conversation-prefix-scorer",
        )
    )

    assert response.status == "clarification"
    assert _get_pending_interaction("conversation-prefix-scorer")[
        "payload"
    ]["invalid_replies"] == 1


def test_new_question_containing_an_option_drops_note_not_selects_it():
    _store_pending_interaction(
        "conversation-new-question",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [{"label": "Drinks & Beverages"}],
            "clarification_question": "Which subcategory do you mean?",
        },
    )

    response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "Show shops in Chennai that sell Drinks & Beverages",
            "request-new-question",
            "conversation-new-question",
        )
    )

    assert response is None
    assert routed == "Show shops in Chennai that sell Drinks & Beverages"
    assert _get_pending_interaction("conversation-new-question") is None


def test_leading_yes_is_stripped_before_routing_a_new_question():
    _store_pending_interaction(
        "conversation-prefixed-new-question",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [{"label": "Drinks & Beverages"}],
            "clarification_question": "Which subcategory do you mean?",
        },
    )

    response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "yes, show shops in Chennai",
            "request-prefixed-new-question",
            "conversation-prefixed-new-question",
        )
    )

    assert response is None
    assert routed == "show shops in Chennai"
    assert _get_pending_interaction(
        "conversation-prefixed-new-question"
    ) is None


def test_yes_no_confirmation_continues_original_question_as_confirmed():
    _store_pending_interaction(
        "conversation-yes-no",
        "clarification",
        {
            "original_question": "List bakeries in Nagercoil",
            "kind": "yes/no",
            "unclear_phrase": "",
            "options": [],
            "invalid_replies": 0,
            "asked_items": [],
            "confirmation_complete": True,
            "clarification_question": "Do you want to search for bakeries?",
        },
    )

    response, continued = asyncio.run(
        chat._handle_pending_or_sql(
            "yes", "request-yes-no", "conversation-yes-no"
        )
    )

    assert response is None
    assert continued.startswith("List bakeries in Nagercoil")
    assert "The user confirmed: yes." in continued


def test_yes_no_confirmation_with_incomplete_filter_does_not_continue():
    _store_pending_interaction(
        "conversation-incomplete-confirmation",
        "clarification",
        {
            "original_question": "Show shops in Nagercoil",
            "kind": "yes/no",
            "options": [],
            "confirmation_complete": False,
            "clarification_question": "Do you want shops in Nagercoil?",
        },
    )

    response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "yes",
            "request-incomplete-confirmation",
            "conversation-incomplete-confirmation",
        )
    )

    assert response.status == "clarification"
    assert response.message == "Do you want shops in Nagercoil?"
    assert routed == "yes"
    assert _get_pending_interaction(
        "conversation-incomplete-confirmation"
    )["payload"]["invalid_replies"] == 1


def test_yes_no_legacy_note_without_confirmation_flag_is_not_complete():
    _store_pending_interaction(
        "conversation-legacy-confirmation",
        "clarification",
        {
            "original_question": "Show shops in Nagercoil",
            "kind": "yes/no",
            "options": [],
            "clarification_question": "Do you want shops in Nagercoil?",
        },
    )

    response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "yes",
            "request-legacy-confirmation",
            "conversation-legacy-confirmation",
        )
    )

    assert response.status == "clarification"
    assert routed == "yes"
    assert _get_pending_interaction(
        "conversation-legacy-confirmation"
    )["payload"]["invalid_replies"] == 1


def test_yes_to_yes_no_with_missing_filter_is_invalid_not_confirmed():
    _store_pending_interaction(
        "conversation-confirmation-missing-filter",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "yes/no",
            "unclear_phrase": "juice",
            "asked_items": ["juice"],
            "options": [],
            "invalid_replies": 0,
            "clarification_question": "Do you mean 'juice'?",
        },
    )

    response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "yes",
            "request-confirmation-missing-filter",
            "conversation-confirmation-missing-filter",
        )
    )

    assert response.status == "clarification"
    assert response.message == "Do you mean 'juice'?"
    assert routed == "yes"
    assert _get_pending_interaction(
        "conversation-confirmation-missing-filter"
    )["payload"]["invalid_replies"] == 1


def test_no_to_yes_no_cancels_and_asks_user_to_rephrase():
    _store_pending_interaction(
        "conversation-cancel-confirmation",
        "clarification",
        {
            "original_question": "List bakeries in Nagercoil",
            "kind": "yes/no",
            "options": [],
            "invalid_replies": 0,
            "clarification_question": "Do you want to search for bakeries?",
        },
    )

    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "no",
            "request-cancel-confirmation",
            "conversation-cancel-confirmation",
        )
    )

    assert response.status == "chat"
    assert "rephrase" in response.message
    assert _get_pending_interaction("conversation-cancel-confirmation") is None


def test_yes_no_prompt_rejects_a_non_yes_no_short_reply():
    _store_pending_interaction(
        "conversation-yes-no-invalid",
        "clarification",
        {
            "original_question": "List bakeries in Nagercoil",
            "kind": "yes/no",
            "options": [],
            "invalid_replies": 0,
            "clarification_question": "Do you want to search for bakeries?",
        },
    )

    response, routed = asyncio.run(
        chat._handle_pending_or_sql(
            "shops", "request-yes-no-invalid", "conversation-yes-no-invalid"
        )
    )

    assert response.status == "clarification"
    assert response.message == "Do you want to search for bakeries?"
    assert routed == "shops"
    assert _get_pending_interaction("conversation-yes-no-invalid")[
        "payload"
    ]["invalid_replies"] == 1


def test_short_reply_to_no_options_note_is_invalid():
    _store_pending_interaction(
        "conversation-no-options",
        "clarification",
        {
            "original_question": "Which category is relevant?",
            "kind": "choose-one",
            "options": [],
            "invalid_replies": 0,
            "clarification_question": "Which category do you mean?",
        },
    )

    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "food",
            "request-no-options",
            "conversation-no-options",
        )
    )

    assert response.status == "clarification"
    assert _get_pending_interaction("conversation-no-options")["payload"][
        "invalid_replies"
    ] == 1


def test_simultaneous_replies_cannot_consume_the_same_note_version(monkeypatch):
    conversation_id = "conversation-simultaneous"
    _store_pending_interaction(
        conversation_id,
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [{"label": "Drinks & Beverages"}],
            "clarification_question": "Which subcategory do you mean?",
        },
    )
    both_read_note = Barrier(2)
    get_pending = chat.get_pending_interaction

    async def synchronized_get(conversation):
        pending = await get_pending(conversation)
        if conversation == conversation_id and pending is not None:
            both_read_note.wait(timeout=5)
        return pending

    monkeypatch.setattr(chat, "get_pending_interaction", synchronized_get)

    def reply(request_id):
        return asyncio.run(
            chat._handle_pending_or_sql(
                "Drinks & Beverages",
                request_id,
                conversation_id,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reply, ["request-race-a", "request-race-b"]))

    assert sum(response is None for response, _ in outcomes) == 1
    stale_responses = [
        response for response, _ in outcomes
        if response is not None
    ]
    assert len(stale_responses) == 1
    assert "already handled" in stale_responses[0].message


def test_no_to_choose_one_is_an_invalid_reply_not_a_cancellation():
    _store_pending_interaction(
        "conversation-no-choice",
        "clarification",
        {
            "original_question": "Show juice shops",
            "kind": "choose-one",
            "options": [{"label": "Drinks & Beverages"}],
            "invalid_replies": 0,
            "clarification_question": "Which subcategory do you mean?",
        },
    )

    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "no", "request-no-choice", "conversation-no-choice"
        )
    )

    assert response.status == "clarification"
    assert _get_pending_interaction("conversation-no-choice")["payload"][
        "invalid_replies"
    ] == 1


def test_clarification_is_saved_as_a_structured_note(monkeypatch):
    monkeypatch.setattr(
        chat,
        "get_business_value_catalog",
        lambda: [
            {
                "source_table": "subcategories",
                "value": "Drinks & Beverages",
            }
        ],
    )
    _store_clarification_if_needed(
        {
            "user_question": "Show juice shops in Nagercoil",
            "final_answer": {
                "clarification_question": (
                    "Which subcategory do you mean by 'juice'?"
                ),
                "clarification_options": ["Drinks & Beverages"],
            },
        },
        "conversation-structured-note",
    )

    note = _get_pending_interaction("conversation-structured-note")["payload"]
    assert note["original_question"] == "Show juice shops in Nagercoil"
    assert note["kind"] == "choose-one"
    assert note["unclear_phrase"] == "juice"
    assert note["options"] == [
        {
            "label": "Drinks & Beverages",
            "kind": "catalog",
            "value": "Drinks & Beverages",
            "term": "Drinks & Beverages",
            "level": "subcategory",
        }
    ]
    assert note["invalid_replies"] == 0
    assert note["asked_items"] == ["juice"]
    assert note["note_id"]
    assert note["version"] == 1
    assert note["conversation_id"] == "conversation-structured-note"


def test_yes_no_note_saves_explicit_confirmation_completeness():
    _store_clarification_if_needed(
        {
            "user_question": "Show shops in Nagercoil",
            "final_answer": {
                "clarification_question": "Do you want shops in Nagercoil?",
                "clarification_options": [],
                "confirmation_complete": True,
            },
        },
        "conversation-explicit-confirmation",
    )

    note = _get_pending_interaction(
        "conversation-explicit-confirmation"
    )["payload"]
    assert note["kind"] == "yes/no"
    assert note["confirmation_complete"] is True


def test_classifier_prompt_declares_confirmation_completeness():
    assert '"confirmation_complete"' in SYSTEM_PROMPT
    assert "true only when the question names ONE" in SYSTEM_PROMPT
    assert "Set it to false when unsure." in SYSTEM_PROMPT


def test_resolver_yes_no_confirmation_flag_reaches_pending_note(monkeypatch):
    monkeypatch.setattr(
        classifier_rewrite,
        "get_business_value_catalog",
        lambda: [],
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "get_business_rule_catalog",
        lambda: [],
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "get_llm_service",
        lambda: type("FakeLLM", (), {
            "call_agent_json": lambda *_args, **_kwargs: asyncio.sleep(0, result={})
        })(),
    )
    monkeypatch.setattr(
        classifier_rewrite,
        "_normalize_llm_output",
        lambda *_args: {
            "intent": "clarification",
            "rewritten_question": "Show shops in Nagercoil",
            "complexity": "simple",
            "chat_reply": "",
            "reasoning": "Confirm the definite search.",
            "clarification_question": "Do you want shops in Nagercoil?",
            "clarification_options": [],
            "clarification_topic": "entity",
            "confirmation_complete": True,
            "temporal_intents": [],
            "shop_type_entities": [],
            "matched_business_rule_terms": [],
            "catalog_filter_combiner": None,
        },
    )
    monkeypatch.setenv("USE_QUESTION_REWRITE", "false")

    classifier_result = asyncio.run(
        classifier_rewrite.classify_and_rewrite(
            "Show shops in Nagercoil"
        )
    )
    assert classifier_result["confirmation_complete"] is True

    node_result = asyncio.run(build_and_save_answer({
        "classifier_result": classifier_result,
    }))
    _store_clarification_if_needed(
        {
            "user_question": "Show shops in Nagercoil",
            "final_answer": node_result["final_answer"],
        },
        "conversation-resolver-confirmation",
    )

    note = _get_pending_interaction(
        "conversation-resolver-confirmation"
    )["payload"]
    assert note["kind"] == "yes/no"
    assert note["confirmation_complete"] is True


def test_non_select_sql_is_refused_without_pipeline_routing():
    response, _ = asyncio.run(
        chat._handle_pending_or_sql(
            "GRANT SELECT ON shops TO reader",
            "request-5",
            "conversation-3",
        )
    )

    assert response.status == "chat"
    assert "Only read-only SELECT" in response.message


def test_natural_language_show_request_is_not_misclassified_as_sql():
    response, routed_question = asyncio.run(
        chat._handle_pending_or_sql(
            "Show the shops in that category, ranked by revenue",
            "request-6",
            "conversation-4",
        )
    )

    assert response is None
    assert routed_question == "Show the shops in that category, ranked by revenue"
