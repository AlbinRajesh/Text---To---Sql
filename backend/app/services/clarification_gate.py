"""Exact-string safeguards for pending clarification selections."""

import json
from pathlib import Path
import unicodedata
from typing import Iterable


_WORDS_PATH = (
    Path(__file__).resolve().parent.parent
    / "rules"
    / "clarification_words.json"
)
_WORD_LISTS = json.loads(_WORDS_PATH.read_text(encoding="utf-8"))


def normalize_phrase(
    phrase: str,
    language: str = "en",
    catalog_tokens: frozenset[str] = frozenset(),
) -> str:
    """Normalize spacing and case without removing or reinterpreting words."""
    if language not in _WORD_LISTS:
        raise ValueError(f"Unsupported clarification language: {language!r}")
    normalized = unicodedata.normalize("NFKC", phrase).casefold()
    return " ".join(normalized.split())


def is_askable(
    phrase: str,
    asked_items: Iterable[str],
    language: str = "en",
    catalog_tokens: frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    """Check only that a clarification phrase is non-empty and not repeated."""
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
    """Identify non-business menu entries defined by the UI catalog."""
    if language not in _WORD_LISTS:
        raise ValueError(f"Unsupported clarification language: {language!r}")
    placeholders = _WORD_LISTS[language].get("placeholder_values", [])
    normalized = normalize_phrase(name, language)
    return normalized in placeholders
