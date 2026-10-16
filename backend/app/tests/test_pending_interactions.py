import threading

from app.services.pending_interactions import (
    PENDING_CLARIFICATION_TTL_SECONDS,
    PENDING_CONFIRMATION_TTL_SECONDS,
    clear_all_pending_interactions,
    consume_pending_interaction,
    consume_pending_interaction_if_matches,
    get_pending_interaction,
    store_pending_interaction,
    update_pending_interaction,
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


def test_clarification_note_has_identity_version_and_short_expiry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "app.services.pending_interactions.time.monotonic",
        lambda: clock[0],
    )
    store_pending_interaction(
        "conversation-a",
        "clarification",
        {"original_question": "Compare active and inactive shops by category"},
    )

    note = get_pending_interaction("conversation-a")
    assert note["payload"]["conversation_id"] == "conversation-a"
    assert note["payload"]["note_id"]
    assert note["payload"]["version"] == 1
    assert note["payload"]["expires_at"] > 0

    clock[0] += 10 * 60

    assert PENDING_CLARIFICATION_TTL_SECONDS == 10 * 60
    assert get_pending_interaction("conversation-a") is None


def test_clarification_update_is_compare_and_swap():
    store_pending_interaction(
        "conversation-a",
        "clarification",
        {"original_question": "List drinks", "invalid_replies": 0},
    )
    note = get_pending_interaction("conversation-a")
    note_id = note["interaction_id"]

    assert update_pending_interaction(
        "conversation-a",
        note_id,
        1,
        {"original_question": "List drinks", "invalid_replies": 1},
    )
    assert not update_pending_interaction(
        "conversation-a",
        note_id,
        1,
        {"original_question": "List drinks", "invalid_replies": 1},
    )

    updated = get_pending_interaction("conversation-a")
    assert updated["version"] == 2
    assert updated["payload"]["version"] == 2
    assert updated["payload"]["invalid_replies"] == 1


def test_clarification_note_can_only_be_consumed_for_its_current_version():
    store_pending_interaction(
        "conversation-a",
        "clarification",
        {"original_question": "List drinks"},
    )
    note = get_pending_interaction("conversation-a")

    assert not consume_pending_interaction_if_matches(
        "conversation-a", note["interaction_id"], 2
    )
    assert consume_pending_interaction_if_matches(
        "conversation-a", note["interaction_id"], 1
    )
    assert not consume_pending_interaction_if_matches(
        "conversation-a", note["interaction_id"], 1
    )


def test_concurrent_updates_cannot_both_claim_the_same_note_version():
    store_pending_interaction(
        "conversation-a",
        "clarification",
        {"original_question": "List drinks", "invalid_replies": 0},
    )
    note = get_pending_interaction("conversation-a")
    barrier = threading.Barrier(3)
    results = []

    def update():
        barrier.wait()
        results.append(update_pending_interaction(
            "conversation-a",
            note["interaction_id"],
            note["version"],
            {"original_question": "List drinks", "invalid_replies": 1},
        ))

    workers = [threading.Thread(target=update) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert sorted(results) == [False, True]
    assert get_pending_interaction("conversation-a")["payload"][
        "invalid_replies"
    ] == 1


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
