from app.services import clarification_gate


def test_rejects_number_and_stop_word_phrase():
    assert clarification_gate.normalize_phrase("5 by") == ""
    assert clarification_gate.is_askable("5 by", [])[0] is False
    assert clarification_gate.is_askable("5 by", [])[1] == "junk"


def test_rejects_yes_as_junk():
    assert clarification_gate.normalize_phrase("yes") == ""
    assert clarification_gate.is_askable("yes", []) == (False, "junk")


def test_drops_related_modifier_but_keeps_business_term():
    assert clarification_gate.normalize_phrase("drinks-related") == "drinks"
    assert clarification_gate.normalize_phrase("drinks related") == "drinks"
    assert clarification_gate.normalize_phrase("food related") == "food"


def test_business_phrase_is_askable():
    assert clarification_gate.is_askable("food", []) == (True, "askable")


def test_rejects_phrase_that_was_already_asked():
    assert clarification_gate.is_askable(
        "drinks-related",
        ["drinks"],
    ) == (False, "already_asked")


def test_real_catalog_token_is_preserved_even_if_configured_as_stop_word(
    monkeypatch,
):
    monkeypatch.setitem(
        clarification_gate._WORD_LISTS["en"],
        "stop_words",
        [*clarification_gate._WORD_LISTS["en"]["stop_words"], "dining"],
    )

    assert clarification_gate.normalize_phrase(
        "dining",
        catalog_tokens=frozenset({"dining"}),
    ) == "dining"


def test_recognizes_placeholder_catalog_values_case_insensitively():
    assert clarification_gate.is_placeholder("All")
    assert clarification_gate.is_placeholder("MORE")
    assert not clarification_gate.is_placeholder("Food & Dining")
