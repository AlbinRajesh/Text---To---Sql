from app.services.pending_interactions import (
    PENDING_CLARIFICATION_TTL_SECONDS,
    PENDING_CONFIRMATION_TTL_SECONDS,
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


def test_clarification_reply_window_exceeds_five_minutes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "app.services.pending_interactions.time.monotonic",
        lambda: clock[0],
    )
    store_pending_interaction(
        "conversation-a",
        "clarification",
        {"question": "Compare active and inactive shops by category"},
    )

    clock[0] += 10 * 60

    assert PENDING_CLARIFICATION_TTL_SECONDS == 30 * 60
    assert get_pending_interaction("conversation-a")["type"] == "clarification"


def test_sql_confirmation_retains_short_expiry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "app.services.pending_interactions.time.monotonic",
        lambda: clock[0],
    )
    store_pending_interaction(
        "conversation-a",
        "sql_confirmation",
        {"sql": "SELECT 1"},
    )

    clock[0] += PENDING_CONFIRMATION_TTL_SECONDS

    assert consume_pending_interaction("conversation-a") is None
