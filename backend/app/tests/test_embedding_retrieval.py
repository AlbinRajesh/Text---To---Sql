from app.services.embedding_service import _guaranteed_table_names


def test_explicit_plural_table_mentions_are_guaranteed_in_retrieval():
    assert _guaranteed_table_names(
        "How many offers are currently active?",
        ["offer", "subscriptions", "users"],
    ) == {"offer"}


def test_explicit_singular_table_mentions_match_plural_table_names():
    assert _guaranteed_table_names(
        "Count each subscription",
        ["offer", "subscriptions", "users"],
    ) == {"subscriptions"}
