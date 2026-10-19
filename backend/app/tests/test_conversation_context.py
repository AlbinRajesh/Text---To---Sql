from app.services import conversation_context


def test_conversation_context_is_bounded_and_isolated():
    conversation_context.clear_all_conversation_contexts()
    conversation_context.store_verified_turn(
        "conversation-a",
        columns=["category", "booking_count"],
        rows=[{"category": "Services", "booking_count": 8}],
        metrics=[{"aggregate": "COUNT", "alias": "booking_count"}],
        filters={"table": "bookings"},
    )

    context = conversation_context.get_conversation_context("conversation-a")
    assert context[0]["rows"] == [{"category": "Services", "booking_count": 8}]
    assert conversation_context.get_conversation_context("conversation-b") == []

    context[0]["rows"][0]["category"] = "mutated"
    assert (
        conversation_context.get_conversation_context("conversation-a")[0]["rows"][0]["category"]
        == "Services"
    )


def test_conversation_context_does_not_store_empty_results():
    conversation_context.clear_all_conversation_contexts()
    conversation_context.store_verified_turn(
        "conversation-a",
        columns=["booking_count"],
        rows=[],
        metrics=[],
        filters=None,
    )
    assert conversation_context.get_conversation_context("conversation-a") == []
