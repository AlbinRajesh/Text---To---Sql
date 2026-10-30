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


def test_views_table_matches_view_and_participle_forms():
    assert _guaranteed_table_names(
        "Show me each viewed item",
        ["views"],
    ) == {"views"}
    assert _guaranteed_table_names(
        "Show me each view",
        ["views"],
    ) == {"views"}
    assert _guaranteed_table_names(
        "Show me each viewing",
        ["views"],
    ) == {"views"}


def test_compound_table_prefix_matches_related_tables():
    assert _guaranteed_table_names(
        "Show me shops",
        ["shop_details", "shop_address", "offers"],
    ) == {"shop_details", "shop_address"}


def test_domain_synonyms_guarantee_canonical_tables():
    assert _guaranteed_table_names(
        "Find the best deal",
        ["offer", "shop_details"],
    ) == {"offer"}
    assert _guaranteed_table_names(
        "List stores",
        ["offer", "shop_details"],
    ) == {"shop_details"}


def test_booking_language_guarantees_booking_table():
    assert _guaranteed_table_names(
        "Which service is booked the most?",
        ["service", "offer", "bookings", "reviews"],
    ) == {"bookings", "service"}

    assert _guaranteed_table_names(
        "Count reservations",
        ["bookings", "users"],
    ) == {"bookings"}
