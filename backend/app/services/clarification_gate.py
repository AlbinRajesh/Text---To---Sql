"""Shared phrase checks for deciding whether a clarification is appropriate."""

import json
from pathlib import Path
import re
import unicodedata
from typing import AbstractSet, Iterable


_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_WORDS_PATH = (
    Path(__file__).resolve().parent.parent
    / "rules"
    / "clarification_words.json"
)
_WORD_LISTS = json.loads(_WORDS_PATH.read_text(encoding="utf-8"))


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _TOKEN_PATTERN.findall(normalized)


def _language_words(language: str) -> dict[str, frozenset[str]]:
    if language not in _WORD_LISTS:
        raise ValueError(f"Unsupported clarification language: {language!r}")

    words = _WORD_LISTS[language]
    required = ("stop_words", "modifiers", "placeholder_values")
    if not isinstance(words, dict) or any(
        not isinstance(words.get(key), list)
        or any(not isinstance(item, str) for item in words[key])
        for key in required
    ):
        raise ValueError(
            f"Invalid clarification word-list configuration for {language!r}"
        )
    return {
        key: frozenset(
            token
            for value in words[key]
            for token in _tokens(value)
        )
        for key in required
    }


def normalize_phrase(
    phrase: str,
    language: str = "en",
    catalog_tokens: AbstractSet[str] = frozenset(),
) -> str:
    """Remove non-informative words while retaining real catalog vocabulary."""
    words = _language_words(language)
    protected_tokens = {
        token
        for value in catalog_tokens
        for token in _tokens(value)
    }
    ignored = words["stop_words"] | words["modifiers"]
    return " ".join(
        token
        for token in _tokens(phrase)
        if not token.isdigit()
        and (token in protected_tokens or token not in ignored)
    )


def is_askable(
    phrase: str,
    asked_items: Iterable[str],
    language: str = "en",
    catalog_tokens: AbstractSet[str] = frozenset(),
) -> tuple[bool, str]:
    """Return whether a phrase is meaningful and has not already been asked."""
    if not phrase.strip():
        return (False, "already_asked") if asked_items else (True, "askable")

    normalized = normalize_phrase(phrase, language, catalog_tokens)
    if not normalized:
        return False, "junk"

    previously_asked = {
        normalize_phrase(item, language, catalog_tokens)
        for item in asked_items
    }
    if normalized in previously_asked:
        return False, "already_asked"
    return True, "askable"


def is_placeholder(name: str, language: str = "en") -> bool:
    """Identify non-business catalog entries such as the menu values All/More."""
    placeholders = _language_words(language)["placeholder_values"]
    return " ".join(_tokens(name)) in placeholders
