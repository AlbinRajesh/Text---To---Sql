"""Resolve business-type language against typed database catalog values."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import unicodedata
from typing import Any, Dict, Iterable, Literal, Sequence

from app.services.clarification_gate import (
    is_askable,
    is_placeholder,
    normalize_phrase,
)
from app.services.value_index import (
    get_business_value_catalog,
    resolve_value_candidates,
)
_PROVIDED_SERVICE_CLAUSE = re.compile(
    r"\b(?:provide|provides|providing|offer|offers|offering)\s+"
    r"(.+?)\s+services?\b",
    re.IGNORECASE,
)

CatalogLevel = Literal["category", "subcategory"]
logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")
_NEGATIONS = {"exclude", "excluding", "except", "without", "not"}
_ALIAS_SEARCHES: Dict[str, tuple[str, CatalogLevel | None]] = {
    "restaurant": ("restaurant", "subcategory"),
    "restaurants": ("restaurant", "subcategory"),
    "restaurent": ("restaurant", "subcategory"),
    "resturent": ("restaurant", "subcategory"),
    "food places": ("restaurant", "subcategory"),
    "places to eat": ("restaurant", "subcategory"),
    "dining": ("restaurant", "subcategory"),
    "dining option": ("restaurant", "subcategory"),
    "dining options": ("restaurant", "subcategory"),
    "drink": ("drink", "subcategory"),
    "drinks": ("drink", "subcategory"),
    "snack": ("snack", "subcategory"),
    "snacks": ("snack", "subcategory"),
    "beverage": ("beverage", None),
    "beverages": ("beverage", None),
    "service": ("service", "category"),
    "services": ("service", "category"),
}
_FILLER_WORDS = {
    "a", "an", "the", "any", "some", "me", "my", "their", "there",
    "shop", "shops", "business", "businesses", "place", "places",
    "option", "options", "type", "types", "in", "near", "at", "around",
    "within", "with", "from", "for", "each", "and", "or", "both", "all", "compare",
    "number", "count", "how", "many", "of", "category", "categories",
    "list", "show", "find", "search", "which", "what", "are", "is", "there",
    "have", "has",
}
_REQUEST_QUALIFIERS = {
    "active", "inactive", "best", "top", "new", "open", "closed", "cheap",
    "discounted", "available", "good", "popular", "most", "fewest",
    "highest", "lowest", "total", "average", "avg",
}
_NON_SHOP_ENTITIES = {
    "category", "categories", "subcategory", "subcategories", "offer",
    "offers", "user", "users", "booking", "bookings", "review", "reviews",
    "order", "orders", "plan", "plans", "city", "cities", "location",
    "locations", "address", "addresses", "discount", "discounts",
    "rating", "ratings", "customer", "customers", "revenue", "sales",
}
_SELL_CLAUSE = re.compile(
    r"\b(?:sell|sells|selling|provide|provides|providing)\s+(.+?)"
    r"(?=\s+(?:in|near|around|at|within)\s+[a-z0-9]"
    r"|\s+and\s+(?:what|include|including|their)\b|[,.;?!]|$)",
    re.IGNORECASE,
)
_KNOWN_BUSINESS_TYPE_PATTERN = re.compile(
    r"\b(?:restaurants?|dining|beverages?|drinks?|services?|"
    r"bakery|bakeries|food\s+places|places\s+to\s+eat)\b",
    re.IGNORECASE,
)
_SHOP_ENTITY_PATTERN = re.compile(
    r"\b(?:shops?|businesses|places|options?)\b",
    re.IGNORECASE,
)
_STRUCTURAL_CONNECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "rules" / "clarification_words.json")
    .read_text(encoding="utf-8")
)["en"]["structural_connectors"]
_STRUCTURAL_CONNECTOR_PATTERN = re.compile(
    r"\b(?:" + "|".join(
        re.escape(phrase).replace(r"\ ", r"\s+")
        for phrase in _STRUCTURAL_CONNECTORS
    ) + r")\b",
    re.IGNORECASE,
)
_RULE_WORDS = json.loads(
    (Path(__file__).resolve().parents[1] / "rules" / "clarification_words.json")
    .read_text(encoding="utf-8")
)["en"]
_FILTER_CONNECTORS = {
    connector: {
        tuple(_TOKEN.findall(phrase.casefold()))
        for phrase in phrases
    }
    for connector, phrases in _RULE_WORDS["filter_connectors"].items()
}
_COMPARISON_TRIGGERS = {
    tuple(_TOKEN.findall(phrase.casefold()))
    for phrase in _RULE_WORDS["comparison_triggers"]
}


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "zes", "sses")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return tuple(_stem(token) for token in _TOKEN.findall(normalized))


@dataclass(frozen=True)
class CatalogValue:
    level: CatalogLevel
    value: str


@dataclass(frozen=True)
class _Occurrence:
    start: int
    end: int
    phrase: str
    values: tuple[CatalogValue, ...]
    excluded: bool


def _level_for_table(table: str) -> CatalogLevel:
    return "category" if table == "categories" else "subcategory"


def _alias_values(alias: str, search: str, level: CatalogLevel | None) -> list[CatalogValue]:
    candidates = [
        candidate
        for candidate in resolve_value_candidates(search, limit_per_field=5)
        if candidate.get("source_table") in {"categories", "subcategories"}
        and isinstance(candidate.get("value"), str)
        and (
            level is None
            or _level_for_table(candidate["source_table"]) == level
        )
    ]
    exact = [candidate for candidate in candidates if candidate.get("exact_match") is True]
    if exact:
        candidates = exact
    elif candidates:
        best_rank = max(
            (
                candidate.get("matched_token_count", 0),
                candidate.get("score", 0),
            )
            for candidate in candidates
        )
        if best_rank[1] < 0.9:
            return []
        candidates = [
            candidate
            for candidate in candidates
            if (
                candidate.get("matched_token_count", 0),
                candidate.get("score", 0),
            ) == best_rank
        ]
    return list(dict.fromkeys(
        CatalogValue(_level_for_table(candidate["source_table"]), candidate["value"])
        for candidate in candidates
    ))


def _catalog_phrases() -> Dict[tuple[str, ...], list[CatalogValue]]:
    phrases: Dict[tuple[str, ...], list[CatalogValue]] = {}

    def add(phrase: str, value: CatalogValue) -> None:
        tokens = _tokens(phrase)
        if tokens:
            bucket = phrases.setdefault(tokens, [])
            if value not in bucket:
                bucket.append(value)

    for item in get_business_value_catalog():
        if is_placeholder(item["value"]):
            continue
        add(
            item["value"],
            CatalogValue(_level_for_table(item["source_table"]), item["value"]),
        )
    for alias, (search, level) in _ALIAS_SEARCHES.items():
        for value in _alias_values(alias, search, level):
            add(alias, value)
    return phrases


def _occurrences(question: str) -> list[_Occurrence]:
    phrases = _catalog_phrases()
    tokens = _tokens(question)
    surface_tokens = _TOKEN.findall(unicodedata.normalize("NFKC", question).casefold())
    maximum = max((len(phrase) for phrase in phrases), default=0)
    found: list[_Occurrence] = []
    index = 0
    while index < len(tokens):
        hit = None
        for length in range(min(maximum, len(tokens) - index), 0, -1):
            hit = phrases.get(tokens[index:index + length])
            if hit:
                hit_length = length
                break
        if not hit:
            index += 1
            continue
        found.append(_Occurrence(
            start=index,
            end=index + hit_length,
            phrase=" ".join(surface_tokens[index:index + hit_length]),
            values=tuple(hit),
            excluded=any(
                token in _NEGATIONS
                for token in tokens[max(0, index - 4):index]
            ),
        ))
        index += hit_length
    return found


def _unresolved_provided_service_type(
    question: str,
    occurrences: list[_Occurrence],
) -> str | None:
    matched_tokens = {
        token
        for occurrence in occurrences
        for token in _tokens(occurrence.phrase)
    }
    for clause in _PROVIDED_SERVICE_CLAUSE.findall(question):
        for chunk in re.split(r"\s+(?:and|or)\s+", clause, flags=re.IGNORECASE):
            raw_tokens = _TOKEN.findall(chunk.casefold())
            meaningful = [
                _stem(token)
                for token in raw_tokens
                if _stem(token) not in _FILLER_WORDS
                and _stem(token) not in _REQUEST_QUALIFIERS
            ]
            unresolved = [token for token in meaningful if token not in matched_tokens]
            if unresolved:
                unresolved_set = set(unresolved)
                surface = [
                    token for token in raw_tokens
                    if _stem(token) in unresolved_set
                ]
                return " ".join(surface or unresolved)
    return None


def _clarification_options(values: Iterable[CatalogValue]) -> list[str]:
    values = list(dict.fromkeys(
        item for item in values if not is_placeholder(item.value)
    ))
    value_levels: Dict[str, set[CatalogLevel]] = {}
    for value in values:
        value_levels.setdefault(value.value.casefold(), set()).add(value.level)
    options = []
    for item in values:
        if len(value_levels[item.value.casefold()]) > 1:
            label = f"{item.level.title()}: {item.value}"
        else:
            label = item.value
        if label not in options:
            options.append(label)
    return options[:4]


def _select_ambiguous_values(
    choices: list[CatalogValue],
    reply: str,
) -> list[CatalogValue] | None:
    normalized = reply.strip().casefold()
    if normalized in {"both", "all", "either"}:
        return choices
    number = re.fullmatch(r"(?:option\s*)?(\d+)", normalized)
    if number:
        index = int(number.group(1)) - 1
        return [choices[index]] if 0 <= index < len(choices) else None
    level_match = re.fullmatch(r"(category|subcategory)\s*:\s*(.+)", normalized)
    requested_level = level_match.group(1) if level_match else None
    requested_value = level_match.group(2) if level_match else normalized
    matches = [
        item
        for item in choices
        if item.value.casefold() == requested_value.strip().casefold()
        and (requested_level is None or item.level == requested_level)
    ]
    return matches if len(matches) == 1 else None


def _unresolved_sold_items(
    question: str,
    occurrences: list[_Occurrence],
) -> list[str]:
    """Do not treat a category label as verified inventory for a shop."""
    unresolved = []
    for clause in _SELL_CLAUSE.findall(question):
        if re.search(r"\bservices?\b", clause, re.IGNORECASE):
            continue
        chunks = re.split(r"\s+(?:and|or)\s+", clause, flags=re.IGNORECASE)
        for chunk in chunks:
            chunk_tokens = _tokens(chunk)
            meaningful = [token for token in chunk_tokens if token not in _FILLER_WORDS]
            if not meaningful:
                continue
            if not any(
                _tokens(occurrence.phrase) == tuple(meaningful)
                or set(meaningful).issubset(set(_tokens(occurrence.phrase)))
                for occurrence in occurrences
            ):
                unresolved.append(" ".join(meaningful))
    return list(dict.fromkeys(unresolved))


def _unresolved_coordinated_types(
    question: str,
    occurrences: list[_Occurrence],
) -> list[str]:
    if not re.search(
        r"\b(?:list|show|find|search|compare)\b", question, re.IGNORECASE
    ):
        return []
    unresolved = []
    for part in re.split(r"\s+(?:and|or)\s+", question, flags=re.IGNORECASE):
        if not re.search(
            r"\b(?:shops?|businesses|places|options?)\b", part, re.IGNORECASE
        ):
            continue
        part_tokens = _tokens(part)
        if any(
            _tokens(occurrence.phrase) == part_tokens
            or set(_tokens(occurrence.phrase)).issubset(set(part_tokens))
            for occurrence in occurrences
        ):
            continue
        if re.search(r"\b(?:in|near|at|around|within|from)\b", part, re.IGNORECASE):
            prefix = re.split(
                r"\b(?:in|near|at|around|within|from)\b",
                part,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
        else:
            prefix = part
        prefix = re.sub(
            r"^\s*(?:show|list|find|compare|what|which|are|there|any|me)\b",
            "",
            prefix,
            flags=re.IGNORECASE,
        )
        descriptor = re.sub(
            r"\b(?:shops?|businesses|places|options?)\b",
            "",
            prefix,
            flags=re.IGNORECASE,
        )
        meaningful = [
            token for token in _tokens(descriptor)
            if token not in _FILLER_WORDS and token not in _REQUEST_QUALIFIERS
        ]
        if meaningful:
            unresolved.append(" ".join(meaningful))
    return list(dict.fromkeys(unresolved))


def _unresolved_request_type(
    question: str,
    occurrences: list[_Occurrence],
) -> str | None:
    request = re.search(
        r"\b(?:list|show|find|search|what\s+(?:are|is)|which|"
        r"are\s+there(?:\s+any)?|is\s+there(?:\s+any)?)\s+"
        r"(?:me\s+)?(.+?)(?=\s+(?:in|near|at|around|within|from)\b|[,;?!]|$)",
        question,
        re.IGNORECASE,
    )
    question_shop_entity = _SHOP_ENTITY_PATTERN.search(question)
    if request:
        descriptor = request.group(1)
    elif question_shop_entity:
        descriptor = question[:question_shop_entity.start()]
    else:
        return None
    descriptor = re.split(
        r"\b(?:that\s+(?:sell|sells|provide|provides)|who\s+"
        r"(?:sell|sells|provide|provides)|which\s+(?:sell|sells|"
        r"provide|provides)|with|offering|selling|providing|"
        r"sell|sells|provide|provides|include|including|and\s+what|are|is)\b",
        descriptor,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    descriptor = _STRUCTURAL_CONNECTOR_PATTERN.split(descriptor, maxsplit=1)[0]
    raw_tokens = [token.casefold() for token in _TOKEN.findall(descriptor)]
    meaningful = [
        token for token in _tokens(descriptor)
        if token not in _FILLER_WORDS and token not in _REQUEST_QUALIFIERS
    ]
    if not meaningful or set(meaningful).issubset(_NON_SHOP_ENTITIES):
        return None
    matched_tokens = {
        token
        for occurrence in occurrences
        for token in _tokens(occurrence.phrase)
    }
    unresolved = [token for token in meaningful if token not in matched_tokens]
    if not unresolved:
        return None
    unresolved_surface = [
        token for token in raw_tokens if _stem(token) in set(meaningful)
    ]
    if raw_tokens and raw_tokens[0] in _NON_SHOP_ENTITIES:
        return None
    is_shop_request = any(
        token in {
            "shop", "shops", "business", "businesses", "place", "places",
            "option", "options", "service", "services", "beverage",
            "beverages", "drink", "drinks", "restaurant", "restaurants",
            "bakery", "bakeries", "food", "dining",
        }
        for token in raw_tokens
    ) or any(
        re.search(r"(?:ies|ists|ers|ians)$", token) and len(token) > 4
        for token in raw_tokens
    )
    if not is_shop_request and question_shop_entity is None:
        return None
    return " ".join(unresolved_surface or unresolved)


def _should_resolve_business_types(question: str) -> bool:
    if _SELL_CLAUSE.search(question):
        return True
    if re.search(
        r"\b(?:in|under|within)\s+(?:the\s+)?(?:sub)?categor(?:y|ies)"
        r"(?:\s+(?:named|called|is))?\b",
        question,
        re.IGNORECASE,
    ):
        return True

    entity = _SHOP_ENTITY_PATTERN.search(question)
    if entity:
        prefix_tokens = _tokens(question[:entity.start()])
        meaningful_prefix = [
            token for token in prefix_tokens
            if token not in _FILLER_WORDS
            and token not in _REQUEST_QUALIFIERS
            and token not in _NON_SHOP_ENTITIES
        ]
        if meaningful_prefix:
            return True
        if re.search(r"\bfor\s+\w+", question[entity.end():], re.IGNORECASE):
            return True
        return bool(_KNOWN_BUSINESS_TYPE_PATTERN.search(question))

    request = re.search(
        r"\b(?:list|show|find|search|which|are\s+there(?:\s+any)?)\s+"
        r"(?:me\s+)?(.+?)(?=\s+(?:in|near|at|around|within|from)\b|[,;?!]|$)",
        question,
        re.IGNORECASE,
    )
    if not request:
        return False
    raw_tokens = _TOKEN.findall(request.group(1).casefold())
    if raw_tokens and raw_tokens[0] in _NON_SHOP_ENTITIES:
        return False
    return bool(
        _KNOWN_BUSINESS_TYPE_PATTERN.search(request.group(1))
        or any(
            re.search(r"(?:ies|ists|ers|ians)$", token) and len(token) > 4
            for token in raw_tokens
        )
    )


def _catalog_tokens(phrases: Dict[tuple[str, ...], list[CatalogValue]]) -> frozenset[str]:
    return frozenset(token for phrase in phrases for token in phrase)


def _connector_between(
    question: str,
    left: _Occurrence,
    right: _Occurrence,
) -> str | None:
    token_matches = list(re.finditer(r"[a-z0-9]+", question, re.IGNORECASE))
    if left.end <= left.start or right.end <= right.start:
        return None
    if left.end > len(token_matches) or right.start >= len(token_matches):
        return None
    connector_text = question[
        token_matches[left.end - 1].end():token_matches[right.start].start()
    ]
    connector = tuple(_TOKEN.findall(connector_text.casefold()))
    if not connector:
        return None
    matches = [
        name for name, phrases in _FILTER_CONNECTORS.items()
        if connector in phrases
    ]
    return matches[0] if len(matches) == 1 else None


def _request_combiner(
    question: str,
    occurrences: Sequence[_Occurrence],
    filters: Sequence[Dict[str, Any]],
) -> str | None:
    positioned = sorted(
        (occurrence for occurrence in occurrences if occurrence.end > occurrence.start),
        key=lambda occurrence: occurrence.start,
    )
    connectors = [
        _connector_between(question, left, right)
        for left, right in zip(positioned, positioned[1:])
    ]
    if not connectors and len(filters) > 1:
        return "OR" if len(positioned) == 1 else None
    if not connectors or any(connector is None for connector in connectors):
        return None if len(filters) > 1 else "AND"
    unique_connectors = set(connectors)
    if len(unique_connectors) != 1:
        return None
    return connectors[0].upper()


def _has_mixed_filter_connectors(question: str) -> bool:
    connectors = set()
    for clause in _SELL_CLAUSE.findall(question):
        tokens = tuple(_TOKEN.findall(clause.casefold()))
        for connector, phrases in _FILTER_CONNECTORS.items():
            for phrase in phrases:
                width = len(phrase)
                if any(
                    tokens[index:index + width] == phrase
                    for index in range(len(tokens) - width + 1)
                ):
                    connectors.add(connector)
    return len(connectors) > 1


def _is_comparison_request(question: str) -> bool:
    tokens = tuple(_TOKEN.findall(question.casefold()))
    return any(
        tokens[index:index + len(trigger)] == trigger
        for trigger in _COMPARISON_TRIGGERS
        for index in range(len(tokens) - len(trigger) + 1)
    )


def _checked_clarification_phrase(
    phrase: str,
    asked_items: Sequence[str],
    catalog_tokens: frozenset[str],
) -> tuple[str, str]:
    normalized = normalize_phrase(phrase, catalog_tokens=catalog_tokens)
    askable, reason = is_askable(
        phrase,
        asked_items,
        catalog_tokens=catalog_tokens,
    )
    if not askable:
        logger.info("clarification_gate rejected reason=%s", reason)
    return normalized, reason if not askable else "askable"


def _clarification_refusal(phrase: str) -> Dict[str, Any]:
    return {
        "intent": "chat",
        "rewritten_question": "",
        "clarification_question": "",
        "clarification_options": [],
        "catalog_filters": [],
        "chat_reply": (
            f"I can't ask about '{phrase}' again. Please rephrase your "
            "request with the shop type you mean."
        ),
        "reasoning": "A clarification item was already asked in this conversation.",
    }


def resolve_business_request(
    question: str,
    asked_items: Sequence[str] = (),
) -> Dict[str, Any] | None:
    """Return grounded shop filters, or a catalog-backed clarification."""
    standalone = re.split(
        r"\n\s*Clarification:", question, maxsplit=1, flags=re.IGNORECASE
    )[0].strip()
    is_comparison = _is_comparison_request(standalone)
    should_resolve = _should_resolve_business_types(standalone)
    if not is_comparison and not should_resolve:
        return None
    phrases = _catalog_phrases()
    catalog_tokens = _catalog_tokens(phrases)
    occurrences = _occurrences(standalone)
    if is_comparison and len(occurrences) > 1:
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                "I can't compare categories in one answer yet. Please ask "
                "for each one separately."
            ),
            "clarification_options": [],
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "Comparisons between categories are not supported.",
        }
    if not should_resolve:
        return None
    if _has_mixed_filter_connectors(standalone):
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                "Please say whether you mean shops in either category "
                "(or) or shops in both categories (and)."
            ),
            "clarification_options": [
                "Shops in either category (or)",
                "Rephrase the request",
            ],
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "The request uses mixed connectors between shop types.",
        }
    coordinated_types = _unresolved_coordinated_types(standalone, occurrences)
    request_type = _unresolved_request_type(standalone, occurrences)
    provided_service_type = _unresolved_provided_service_type(
        standalone, occurrences
    )
    unresolved_types = []
    resolved_phrases = set()
    normalized_request = None
    not_found_phrase = None

    def resolve_unresolved_phrase(phrase: str) -> tuple[str, str]:
        nonlocal not_found_phrase
        normalized, reason = _checked_clarification_phrase(
            phrase, asked_items, catalog_tokens
        )
        if reason != "askable":
            return normalized, reason
        if normalized in resolved_phrases:
            return normalized, "resolved"
        matched_values = _alias_values(normalized, normalized, None)
        if matched_values:
            occurrences.append(_Occurrence(
                0,
                0,
                normalized,
                tuple(matched_values),
                False,
            ))
            resolved_phrases.add(normalized)
            return normalized, "resolved"
        if normalized not in unresolved_types:
            unresolved_types.append(normalized)
        not_found_phrase = normalized
        return normalized, "unresolved"

    if request_type:
        normalized_request, reason = resolve_unresolved_phrase(request_type)
        if reason == "already_asked":
            return _clarification_refusal(normalized_request)
        if reason == "junk":
            normalized_request = None

    for phrase in coordinated_types:
        normalized, reason = resolve_unresolved_phrase(phrase)
        if reason == "already_asked":
            return _clarification_refusal(normalized)

    if provided_service_type:
        normalized, reason = resolve_unresolved_phrase(provided_service_type)
        if reason == "already_asked":
            return _clarification_refusal(normalized)

    selected_replies = re.findall(
        r"\n\s*Clarification:\s*The user selected:\s*([^\n.]+)",
        question,
        re.IGNORECASE,
    )
    if unresolved_types and selected_replies:
        selected_occurrences = _occurrences(selected_replies[-1])
        if selected_occurrences:
            occurrences.extend(selected_occurrences)
            unresolved_types = []
            normalized_request = None
            not_found_phrase = None
    if not_found_phrase and not_found_phrase in unresolved_types:
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                "I couldn't find a shop type like "
                f"'{not_found_phrase}'."
            ),
            "clarification_options": [],
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "No catalog candidates matched the normalized shop type.",
        }
    if unresolved_types:
        term = unresolved_types[0]
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": f"I couldn't find a shop type like '{term}'.",
            "clarification_options": [],
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "A requested shop type could not be grounded in the database catalog.",
        }
    if not occurrences:
        return None

    ambiguity = next(
        (occurrence for occurrence in occurrences if len(occurrence.values) > 1),
        None,
    )
    if ambiguity:
        normalized, reason = _checked_clarification_phrase(
            ambiguity.phrase, asked_items, catalog_tokens
        )
        if reason == "already_asked":
            return _clarification_refusal(normalized)
        if reason == "junk":
            occurrences.remove(ambiguity)
            ambiguity = None
    chosen_values = None
    chosen_index = None
    if ambiguity and selected_replies:
        chosen_values = _select_ambiguous_values(
            list(ambiguity.values), selected_replies[-1]
        )
        if chosen_values is not None:
            chosen_index = occurrences.index(ambiguity)
            occurrences[chosen_index] = _Occurrence(
                ambiguity.start,
                ambiguity.end,
                ambiguity.phrase,
                tuple(chosen_values),
                ambiguity.excluded,
            )

    ambiguity = next(
        (
            occurrence for index, occurrence in enumerate(occurrences)
            if len(occurrence.values) > 1 and index != chosen_index
        ),
        None,
    )
    if ambiguity:
        options = _clarification_options(ambiguity.values)
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                f"Which category or subcategory do you mean by "
                f"'{ambiguity.phrase}'?"
            ),
            "clarification_options": options,
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "Multiple typed catalog values match the requested business term.",
        }

    unresolved_items = _unresolved_sold_items(standalone, occurrences)
    checked_sold_items = []
    for item in unresolved_items:
        normalized, reason = _checked_clarification_phrase(
            item, asked_items, catalog_tokens
        )
        if reason == "already_asked":
            return _clarification_refusal(normalized)
        if reason == "askable":
            checked_sold_items.append(normalized)
    unresolved_items = checked_sold_items
    if unresolved_items and selected_replies:
        selected_occurrences = _occurrences(selected_replies[-1])
        if selected_occurrences:
            occurrences.extend(selected_occurrences)
            unresolved_items = []
    if unresolved_items:
        item = unresolved_items[0]
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                f"The category catalog does not verify which shops sell "
                f"'{item}'. Which category or subcategory should I use instead?"
            ),
            "clarification_options": [],
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "Product inventory is not verified by category membership.",
        }
    answer_ambiguity = next(
        (
            occurrence for index, occurrence in enumerate(occurrences)
            if len(occurrence.values) > 1 and index != chosen_index
        ),
        None,
    )
    if answer_ambiguity:
        normalized, reason = _checked_clarification_phrase(
            answer_ambiguity.phrase, asked_items, catalog_tokens
        )
        if reason == "already_asked":
            return _clarification_refusal(normalized)
        if reason == "junk":
            occurrences.remove(answer_ambiguity)
            answer_ambiguity = None
    if answer_ambiguity:
        options = _clarification_options(answer_ambiguity.values)
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                "Which category or subcategory do you mean by "
                f"'{answer_ambiguity.phrase}'?"
            ),
            "clarification_options": options,
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "The selected catalog value is ambiguous across types or levels.",
        }

    filters = []
    include_occurrences = []
    for occurrence in occurrences:
        included_occurrence = False
        for value in occurrence.values:
            if (
                _PROVIDED_SERVICE_CLAUSE.search(standalone)
                and value.level == "category"
                and _tokens(value.value) == ("service",)
            ):
                continue
            item = {
                "level": value.level,
                "value": value.value,
                "exclude": occurrence.excluded,
            }
            if item not in filters:
                filters.append(item)
                if not occurrence.excluded:
                    included_occurrence = True
        if included_occurrence:
            include_occurrences.append(occurrence)

    if not filters:
        return None

    include = [item for item in filters if not item["exclude"]]
    exclude = [item for item in filters if item["exclude"]]
    combiner = _request_combiner(
        standalone, include_occurrences, include
    )
    if len(include) > 1 and combiner is None:
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                "Please say whether you mean shops in either category "
                "(or) or shops in both categories (and)."
            ),
            "clarification_options": [
                "Shops in either category (or)",
                "Rephrase the request",
            ],
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "The request's connector between resolved catalog values is missing or ambiguous.",
        }
    if (
        combiner == "AND"
        and len(include) > 1
        and len({item["level"] for item in include}) == 1
    ):
        level = include[0]["level"]
        noun = "subcategory" if level == "subcategory" else "category"
        return {
            "intent": "clarification",
            "rewritten_question": standalone,
            "clarification_question": (
                f"A shop belongs to one {noun}, so 'and' would return no shops. "
                "Did you mean shops in either one?"
            ),
            "clarification_options": [
                "Shops in either one (or)",
                "Rephrase the request",
            ],
            "clarification_kind": "choose_one",
            "catalog_filters": [],
            "reasoning": "An AND across values of the same catalog level cannot match one shop.",
        }
    filter_lines = []
    if include:
        filter_lines.append(
            "include "
            + ("any of " if len(include) > 1 else "")
            + " or ".join(
                f"{item['level']} {item['value']!r}" for item in include
            )
        )
    if exclude:
        filter_lines.append(
            "exclude " + " and ".join(
                f"{item['level']} {item['value']!r}" for item in exclude
            )
        )
    rewritten = (
        standalone
        + "\nVerified business-type filters: "
        + "; ".join(filter_lines)
        + "."
    )
    return {
        "intent": "data",
        "rewritten_question": rewritten,
        "clarification_question": "",
        "clarification_options": [],
        "catalog_filters": filters,
        "catalog_filter_combiner": combiner or "AND",
        "reasoning": "Business-type filters were resolved against the typed database catalog.",
    }


def validate_catalog_filter_plan(
    query: Dict[str, Any],
    expected_filters: list[Dict[str, Any]],
    catalog: list[Dict[str, Any]] | None = None,
) -> None:
    """Reject plans with catalog filters that contradict resolved business intent."""
    if not expected_filters:
        return
    catalog = catalog if catalog is not None else get_business_value_catalog()
    valid_values = {
        (
            _level_for_table(item["source_table"]),
            item["value"].casefold(),
        )
        for item in catalog
    }
    conditions: list[Dict[str, Any]] = []

    def visit(expression: Any, under_or: bool = False) -> None:
        if not isinstance(expression, dict):
            return
        if expression.get("boolean") in {"AND", "OR"}:
            for child in expression.get("conditions", []):
                visit(
                    child,
                    under_or or expression.get("boolean") == "OR",
                )
        else:
            operator = str(expression.get("operator", "")).upper()
            if under_or and operator in {"!=", "<>", "NOT IN"}:
                raise ValueError(
                    "Excluded business types must remain AND exclusions, not "
                    "alternatives in an OR filter."
                )
            conditions.append(expression)

    visit(query.get("where_expression"))
    for join in query.get("joins", []):
        for condition in join.get("conditions", []):
            visit(condition)

    catalog_columns = {
        ("categories", "categoriesname"): "category",
        ("subcategories", "subcategoryname"): "subcategory",
    }
    observed: list[tuple[str, str, str]] = []
    for condition in conditions:
        key = (condition.get("table"), condition.get("column"))
        level = catalog_columns.get(key)
        if level is None:
            continue
        values = condition.get("value")
        values = values if isinstance(values, list) else [values]
        operator = str(condition.get("operator", "")).upper()
        for value in values:
            if not isinstance(value, str):
                continue
            if (level, value.casefold()) not in valid_values:
                raise ValueError(
                    f"'{value}' is not a verified {level} catalog value for "
                    f"{key[0]}.{key[1]}."
                )
            observed.append((level, value.casefold(), operator))

    expected_keys = {
        (
            item["level"],
            item["value"].casefold(),
            bool(item.get("exclude")),
        )
        for item in expected_filters
    }
    for level, value, operator in observed:
        excluded = operator in {"!=", "<>", "NOT IN"}
        if (level, value, excluded) not in expected_keys:
            if any(expected_value == value for _, expected_value, _ in expected_keys):
                raise ValueError(
                    f"The requested catalog value '{value}' was applied at the "
                    "wrong catalog level."
                )
            raise ValueError(
                "The query plan contains an unrequested category or subcategory "
                "filter."
            )

    for expected in expected_filters:
        level = expected["level"]
        value = expected["value"].casefold()
        operators = {
            operator
            for observed_level, observed_value, operator in observed
            if observed_level == level and observed_value == value
        }
        valid_operators = {"!=", "<>","NOT IN"} if expected.get("exclude") else {"=", "IN"}
        if not operators.intersection(valid_operators):
            raise ValueError(
                f"The requested {level} filter {expected['value']!r} is missing "
                "or was applied to the wrong catalog level."
            )

    include_filters = [item for item in expected_filters if not item.get("exclude")]
    if len(include_filters) > 1:
        requested = {
            (item["level"], item["value"].casefold())
            for item in include_filters
        }

        def or_branches(expression: Any) -> list[Any] | None:
            if not isinstance(expression, dict):
                return None
            if expression.get("boolean") != "OR":
                return [expression]
            branches = []
            for child in expression.get("conditions", []):
                nested = or_branches(child)
                if nested is None:
                    return None
                branches.extend(nested)
            return branches

        def branch_values(expression: Any) -> set[tuple[str | None, str]] | None:
            if not isinstance(expression, dict):
                return None
            if expression.get("boolean") in {"AND", "OR"}:
                return None
            level = catalog_columns.get(
                (expression.get("table"), expression.get("column"))
            )
            operator = str(expression.get("operator", "")).upper()
            if level is None or operator not in {"=", "IN"}:
                return None
            raw_values = expression.get("value")
            raw_values = (
                raw_values if isinstance(raw_values, list) else [raw_values]
            )
            return {
                (level, value.casefold())
                for value in raw_values
                if isinstance(value, str)
            }

        def contains_or_group(expression: Any) -> bool:
            if not isinstance(expression, dict):
                return False
            if expression.get("boolean") in {"AND", "OR"}:
                children = expression.get("conditions", [])
                if expression.get("boolean") == "OR":
                    branches = or_branches(expression)
                    branch_sets = (
                        [branch_values(branch) for branch in branches]
                        if branches is not None else None
                    )
                    if (
                        branch_sets
                        and all(branch is not None and branch for branch in branch_sets)
                        and set().union(*branch_sets) == requested
                    ):
                        return True
                return any(contains_or_group(child) for child in children)
            if expression.get("operator") == "IN":
                level = catalog_columns.get(
                    (expression.get("table"), expression.get("column"))
                )
                values = expression.get("value")
                if not isinstance(values, list):
                    values = [values]
                return requested.issubset({
                    (level, value.casefold())
                    for value in values
                    if isinstance(value, str)
                })
            return False

        if not contains_or_group(query.get("where_expression")):
            raise ValueError(
                "Multiple requested business types must remain together in an "
                "OR filter."
            )
