import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.agent.nodes import classifier_rewrite
from app.agent.nodes.return_save import build_and_save_answer
from app.agent.prompts.classifier_prompt import SYSTEM_PROMPT
from app.api import chat


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
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-2")["payload"][
        "invalid_replies"
    ] == 1


def test_yes_does_not_loop_an_either_or_clarification_through_the_classifier():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-clarification")["payload"][
        "invalid_replies"
    ] == 1


def test_yes_to_choose_one_reprompts_once_then_closes_note():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-yes-choice")["payload"][
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
    assert chat.get_pending_interaction("conversation-yes-choice") is None


def test_long_exact_catalog_option_is_accepted():
    chat.store_pending_interaction(
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
    chat.store_pending_interaction(
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
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-option-prefix")[
        "payload"
    ]["invalid_replies"] == 1


def test_clear_typo_selects_the_best_clarification_option():
    options = [
        {"label": "Restaurent"},
        {"label": "Real estate"},
    ]
    chat.store_pending_interaction(
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


def test_close_typo_score_tie_is_rejected_by_margin():
    options = [
        {"label": "Drinks & Beverages"},
        {"label": "Drinks and Beverages"},
    ]
    assert chat._fuzzy_clarification_option("drinks beverags", options) is None
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-typo-tie")["payload"][
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
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-prefix-scorer")[
        "payload"
    ]["invalid_replies"] == 1


def test_new_question_containing_an_option_drops_note_not_selects_it():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-new-question") is None


def test_leading_yes_is_stripped_before_routing_a_new_question():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction(
        "conversation-prefixed-new-question"
    ) is None


def test_yes_no_confirmation_continues_original_question_as_confirmed():
    chat.store_pending_interaction(
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
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction(
        "conversation-incomplete-confirmation"
    )["payload"]["invalid_replies"] == 1


def test_yes_no_legacy_note_without_confirmation_flag_is_not_complete():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction(
        "conversation-legacy-confirmation"
    )["payload"]["invalid_replies"] == 1


def test_yes_to_yes_no_with_missing_filter_is_invalid_not_confirmed():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction(
        "conversation-confirmation-missing-filter"
    )["payload"]["invalid_replies"] == 1


def test_no_to_yes_no_cancels_and_asks_user_to_rephrase():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-cancel-confirmation") is None


def test_yes_no_prompt_rejects_a_non_yes_no_short_reply():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-yes-no-invalid")[
        "payload"
    ]["invalid_replies"] == 1


def test_short_reply_to_no_options_note_is_invalid():
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-no-options")["payload"][
        "invalid_replies"
    ] == 1


def test_simultaneous_replies_cannot_consume_the_same_note_version(monkeypatch):
    conversation_id = "conversation-simultaneous"
    chat.store_pending_interaction(
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

    def synchronized_get(conversation):
        pending = get_pending(conversation)
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
    chat.store_pending_interaction(
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
    assert chat.get_pending_interaction("conversation-no-choice")["payload"][
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
    chat._store_clarification_if_needed(
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

    note = chat.get_pending_interaction("conversation-structured-note")["payload"]
    assert note["original_question"] == "Show juice shops in Nagercoil"
    assert note["kind"] == "choose-one"
    assert note["unclear_phrase"] == "juice"
    assert note["options"] == [
        {
            "label": "Drinks & Beverages",
            "value": "Drinks & Beverages",
            "level": "subcategory",
        }
    ]
    assert note["invalid_replies"] == 0
    assert note["asked_items"] == ["juice"]
    assert note["note_id"]
    assert note["version"] == 1
    assert note["conversation_id"] == "conversation-structured-note"


def test_yes_no_note_saves_explicit_confirmation_completeness():
    chat._store_clarification_if_needed(
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

    note = chat.get_pending_interaction(
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
        "resolve_business_request",
        lambda *_args, **_kwargs: {
            "intent": "clarification",
            "rewritten_question": "Show shops in Nagercoil",
            "chat_reply": "",
            "reasoning": "Confirm the definite search.",
            "clarification_question": "Do you want shops in Nagercoil?",
            "clarification_options": [],
            "clarification_kind": "yes/no",
            "confirmation_complete": True,
            "catalog_filters": [],
        },
    )

    classifier_result = asyncio.run(
        classifier_rewrite.classify_and_rewrite(
            "Show shops in Nagercoil"
        )
    )
    assert classifier_result["confirmation_complete"] is True

    node_result = asyncio.run(build_and_save_answer({
        "classifier_result": classifier_result,
    }))
    chat._store_clarification_if_needed(
        {
            "user_question": "Show shops in Nagercoil",
            "final_answer": node_result["final_answer"],
        },
        "conversation-resolver-confirmation",
    )

    note = chat.get_pending_interaction(
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
