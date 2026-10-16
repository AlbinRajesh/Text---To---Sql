import asyncio

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


def test_short_clarification_reply_continues_original_question(monkeypatch):
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

    assert direct_response is None
    assert continued_question.startswith(
        "Which category generated booking revenue?"
    )
    assert "The user selected: plan." in continued_question


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
