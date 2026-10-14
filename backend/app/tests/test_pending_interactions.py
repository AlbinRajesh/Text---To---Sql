from app.services.pending_interactions import (
    clear_all_pending_interactions,
    consume_pending_interaction,
    get_pending_interaction,
    store_pending_interaction,
)


def setup_function():
    clear_all_pending_interactions()


def test_pending_interaction_is_conversation_scoped_and_copied():
    payload = {"sql": "SELECT name FROM shops"}
    store_pending_interaction("conversation-a", "sql_confirmation", payload)
    payload["sql"] = "changed"

    assert get_pending_interaction("conversation-a")["payload"]["sql"] == (
        "SELECT name FROM shops"
    )
    assert get_pending_interaction("conversation-b") is None


def test_pending_interaction_is_consumed_once():
    store_pending_interaction(
        "conversation-a",
        "sql_confirmation",
        {"sql": "SELECT name FROM shops"},
    )

    assert consume_pending_interaction("conversation-a")["type"] == "sql_confirmation"
    assert consume_pending_interaction("conversation-a") is None
