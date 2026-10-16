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
