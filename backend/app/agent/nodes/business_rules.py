"""
business_rules.py  (Node 5: Pull Business Rules)

Looks up which business terms (from business_rules.json) apply to a given
question, so Node 6 can generate a query grounded in the company's actual
definitions instead of the AI guessing what a term means.

Business rules must use the active MySQL query-plan contract. MongoDB
aggregation snippets and unresolved dynamic-date tokens are rejected rather
than forwarded to the SQL query generator.
"""

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)
_ACTIVE_RULEBOOK_SNAPSHOT: ContextVar[
    tuple[object, str, List[Dict[str, Any]], List[Dict[str, Any]]] | None
] = ContextVar("active_rulebook_snapshot", default=None)
_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}
_RULE_CONNECTIVE_STOPWORDS = {
    "a", "an", "the", "are", "but", "do", "does", "for", "have",
    "has", "how", "many", "of", "on", "our", "who", "with", "we",
}

RULES_FILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "rules", "business_rules.json"
)

def _normalized_phrase_tokens(value: str) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    normalized = []
    for token in tokens:
        if token in _RULE_CONNECTIVE_STOPWORDS:
            continue
        token = _NUMBER_WORDS.get(token, token)
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        normalized.append(token)
    return tuple(normalized)


def _contains_rule_phrase(question: str, phrase: str) -> bool:
    question_tokens = _normalized_phrase_tokens(question)
    phrase_tokens = _normalized_phrase_tokens(phrase)
    return bool(phrase_tokens) and any(
        question_tokens[index:index + len(phrase_tokens)] == phrase_tokens
        for index in range(len(question_tokens) - len(phrase_tokens) + 1)
    )


def _valid_result_policy(policy: Any) -> bool:
    if not isinstance(policy, dict) or set(policy) != {"limit", "order_by"}:
        return False
    limit = policy.get("limit")
    order_by = policy.get("order_by")
    return (
        type(limit) is int
        and 1 <= limit <= 500
        and isinstance(order_by, list)
        and all(
            isinstance(item, dict)
            and set(item) == {"table", "column", "direction"}
            and all(
                isinstance(item.get(key), str) and item[key]
                for key in ("table", "column")
            )
            and item.get("direction") in {"ASC", "DESC"}
            for item in order_by
        )
    )


class BusinessRulesError(Exception):
    """Raised when the rulebook file is missing, unreadable, or malformed."""
    pass


class BusinessRulesStore:
    """
    Keeps a validated business-rule snapshot in memory.
    The checked-in JSON file is the fallback; a published admin revision can
    replace the snapshot without restarting the application.
    """

    def __init__(self, file_path: str = RULES_FILE_PATH) -> None:
        self._file_path = file_path
        self._terms: List[Dict[str, Any]] = []
        self._ontology_entities: List[Dict[str, Any]] = []
        self._loaded = False
        self._version: Optional[str] = None
        self._document: Optional[Dict[str, Any]] = None

    def _load(self) -> None:
        if not os.path.exists(self._file_path):
            raise BusinessRulesError(
                f"Rulebook not found at {self._file_path}. "
                f"Expected backend/app/rules/business_rules.json."
            )
        try:
            with open(self._file_path, "rb") as f:
                raw = f.read()
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BusinessRulesError(f"Rulebook file is not valid JSON: {e}") from e
        except OSError as e:
            raise BusinessRulesError(f"Could not read business rulebook: {e}") from e

        self.load_data(data, raw)

    def load_data(
        self,
        data: Dict[str, Any],
        source_bytes: bytes | None = None,
    ) -> None:
        """Validate and atomically replace the in-memory rulebook snapshot."""
        if source_bytes is None:
            try:
                source_bytes = json.dumps(
                    data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as e:
                raise BusinessRulesError(
                    f"Rulebook cannot be serialized as JSON: {e}"
                ) from e

        if not isinstance(data, dict):
            raise BusinessRulesError("Rulebook root must be a JSON object.")
        metadata = data.get("_meta")
        if (
            not isinstance(metadata, dict)
            or metadata.get("database") != "MySQL"
            or not isinstance(metadata.get("version"), str)
        ):
            raise BusinessRulesError(
                "Rulebook metadata must identify MySQL and a string version."
            )

        terms = data.get("terms")
        if not isinstance(terms, list):
            raise BusinessRulesError(
                "Rulebook file is missing a top-level 'terms' array."
            )

        ontology_entities = data.get("ontology_entities", [])
        if not isinstance(ontology_entities, list):
            raise BusinessRulesError("'ontology_entities' must be an array.")
        validated_entities = []
        seen_entities = set()
        ontology_entity_fields = {
            "entity", "synonyms", "canonical_table", "temporal_column",
            "filters", "related_entities", "definition",
        }
        for entity in ontology_entities:
            if (
                not isinstance(entity, dict)
                or set(entity) not in (
                    ontology_entity_fields,
                    ontology_entity_fields | {"result_policy"},
                )
                or (
                    "result_policy" in entity
                    and not _valid_result_policy(entity["result_policy"])
                )
            ):
                raise BusinessRulesError("Each ontology entity has an invalid shape.")
            name = entity.get("entity")
            synonyms = entity.get("synonyms")
            filters = entity.get("filters")
            related = entity.get("related_entities")
            if (
                not isinstance(name, str) or not name or name in seen_entities
                or not isinstance(entity.get("canonical_table"), str)
                or not entity["canonical_table"]
                or not isinstance(entity.get("temporal_column"), str)
                or not entity["temporal_column"]
                or not isinstance(entity.get("definition"), str)
                or not entity["definition"].strip()
                or not isinstance(synonyms, list) or not synonyms
                or any(not isinstance(value, str) or not value.strip() for value in synonyms)
                or not isinstance(filters, list)
                or any(
                    not isinstance(item, dict)
                    or set(item) not in (
                        {"column", "operator", "value"},
                        {"table", "column", "operator", "value"},
                        {"column", "operator"},
                        {"table", "column", "operator"},
                    )
                    or not isinstance(item.get("column"), str)
                    or (
                        "table" in item
                        and (
                            not isinstance(item.get("table"), str)
                            or not item["table"]
                        )
                    )
                    or item.get("operator") not in {
                        "=", "!=", ">", ">=", "<", "<=", "IS NULL",
                        "IS NOT NULL",
                    }
                    or (
                        item.get("operator") in {"IS NULL", "IS NOT NULL"}
                        and "value" in item
                    )
                    or (
                        item.get("operator") not in {"IS NULL", "IS NOT NULL"}
                        and "value" not in item
                    )
                    or isinstance(item.get("value"), (dict, list, bool))
                    for item in filters
                )
                or not isinstance(related, list)
                or any(
                    not isinstance(item, dict)
                    or set(item) not in (
                        {"entity", "table", "relationship"},
                        {"entity", "table", "relationship", "required"},
                        {"entity", "table"},
                        {"entity", "table", "required"},
                    )
                    or any(
                        not isinstance(item.get(key), str) or not item[key]
                        for key in ("entity", "table")
                    )
                    or (
                        "relationship" in item
                        and (
                            not isinstance(item.get("relationship"), str)
                            or not item["relationship"]
                        )
                    )
                    or (
                        "required" in item
                        and not isinstance(item["required"], bool)
                    )
                    for item in related
                )
            ):
                raise BusinessRulesError(f"Invalid ontology entity: {name!r}.")
            seen_entities.add(name)
            validated_entities.append(entity)

        validated_terms = []
        seen_terms = set()
        for entry in terms:
            if not isinstance(entry, dict):
                raise BusinessRulesError("Each business rule must be a JSON object.")
            term = entry.get("term")
            synonyms = entry.get("synonyms")
            definition = entry.get("definition")
            logic = entry.get("query_logic", {})
            if (
                not isinstance(term, str)
                or not term.strip()
                or term in seen_terms
                or not isinstance(synonyms, list)
                or not synonyms
                or any(not isinstance(value, str) or not value.strip() for value in synonyms)
                or not isinstance(definition, str)
                or not definition.strip()
                or not isinstance(logic, dict)
                or set(logic) - {
                    "where_expression",
                    "having_expression",
                    "semantic_contract",
                }
            ):
                raise BusinessRulesError(
                    f"Invalid or unsupported SQL business rule: {term!r}."
                )
            excluded_phrases = entry.get("exclude_if_contains", [])
            if (
                not isinstance(excluded_phrases, list)
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in excluded_phrases
                )
            ):
                raise BusinessRulesError(
                    f"Invalid exclusion phrases for SQL business rule {term!r}."
                )
            catalog_resolution = entry.get("catalog_resolution")
            if catalog_resolution not in (None, "rule_defined"):
                raise BusinessRulesError(
                    f"Invalid catalog resolution for SQL business rule {term!r}."
                )
            search_operation = entry.get("search_operation")
            if search_operation is not None and (
                not isinstance(search_operation, dict)
                or set(search_operation) != {"term", "scope"}
                or not isinstance(search_operation.get("term"), str)
                or not search_operation["term"].strip()
                or not isinstance(search_operation.get("scope"), str)
                or search_operation.get("scope")
                not in {"shops", "shop_names", "shop_subcategories"}
            ):
                raise BusinessRulesError(
                    f"Invalid search operation for SQL business rule {term!r}."
                )
            search_scope = entry.get("search_scope")
            if search_scope is not None and (
                not isinstance(search_scope, str)
                or search_scope
                not in {"shops", "shop_names", "shop_subcategories"}
            ):
                raise BusinessRulesError(
                    f"Invalid search scope for SQL business rule {term!r}."
                )
            contract = logic.get("semantic_contract")
            common_contract_fields = {
                "kind",
                "source_table",
                "entity_table",
                "entity_key",
                "relationship",
                "relationship_column",
                "measure_column",
                "aggregate",
                "operator",
                "threshold_pattern",
            }
            list_contract_fields = {
                "entity_label_column",
                "measure_alias",
                "count_column",
                "count_alias",
            }
            contract_kind = contract.get("kind") if isinstance(contract, dict) else None
            contract_fields = common_contract_fields | (
                list_contract_fields
                if contract_kind == "list_entities_by_group_aggregate_threshold"
                else set()
            )
            if contract is not None and (
                not isinstance(contract, dict)
                or set(contract) != contract_fields
                or contract_kind
                not in {
                    "count_entities_by_group_aggregate_threshold",
                    "list_entities_by_group_aggregate_threshold",
                }
                or any(
                    not isinstance(contract.get(key), str)
                    or not contract[key]
                    for key in (
                        "source_table",
                        "entity_table",
                        "entity_key",
                        "relationship",
                        "relationship_column",
                        "measure_column",
                        "aggregate",
                        "operator",
                        "threshold_pattern",
                    )
                )
                or contract.get("aggregate") not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}
                or contract.get("operator") not in {">", ">=", "<", "<=", "=", "!="}
            ):
                raise BusinessRulesError(
                    f"Invalid semantic contract for SQL business rule {term!r}."
                )
            if contract is not None:
                try:
                    threshold_pattern = re.compile(contract["threshold_pattern"], re.IGNORECASE)
                except re.error as exc:
                    raise BusinessRulesError(
                        f"Invalid threshold pattern for SQL business rule {term!r}."
                    ) from exc
                if threshold_pattern.groups != 1:
                    raise BusinessRulesError(
                        f"Threshold pattern for SQL business rule {term!r} must capture exactly one value."
                    )
            if any(
                isinstance(value, str) and "$$" in value
                for value in _walk_values(logic)
            ):
                raise BusinessRulesError(
                    f"Business rule {term!r} contains an unresolved dynamic token."
                )
            entity_resolution = entry.get("entity_resolution")
            if (
                isinstance(entity_resolution, dict)
                and "result_policy" in entity_resolution
                and not _valid_result_policy(entity_resolution["result_policy"])
            ):
                raise BusinessRulesError(
                    f"Invalid result policy for SQL business rule {term!r}."
                )
            seen_terms.add(term)
            validated_terms.append(entry)

        try:
                document = json.loads(json.dumps(data, ensure_ascii=False))
        except (TypeError, ValueError) as e:
                raise BusinessRulesError(
                    f"Rulebook cannot be copied as JSON: {e}"
                ) from e
        self._terms = validated_terms
        self._ontology_entities = validated_entities
        self._document = document
        self._version = hashlib.sha256(source_bytes).hexdigest()
        self._loaded = True
        logger.info(
            "Loaded %d business rule term(s) into the active snapshot.",
            len(terms),
        )

    def export_rulebook(self) -> Dict[str, Any]:
        """Return a detached copy of the validated active rulebook."""
        self._ensure_loaded()
        if self._document is None:
            raise BusinessRulesError("The active rulebook document is unavailable.")
        return json.loads(json.dumps(self._document))

    @contextmanager
    def request_snapshot(self) -> Iterator[str]:
        """Pin one validated rule version for the duration of a request."""
        self._ensure_loaded()
        if self._version is None:
            raise BusinessRulesError("The active rulebook version is unavailable.")
        token = _ACTIVE_RULEBOOK_SNAPSHOT.set(
            (self, self._version, self._terms, self._ontology_entities)
        )
        try:
            yield self._version
        finally:
            _ACTIVE_RULEBOOK_SNAPSHOT.reset(token)

    def reload(self) -> None:
        """Forces a re-read of the rulebook from disk."""
        self._load()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    @property
    def version(self) -> str:
        snapshot = _ACTIVE_RULEBOOK_SNAPSHOT.get()
        if snapshot is not None and snapshot[0] is self:
            return snapshot[1]
        self._ensure_loaded()
        if self._version is None:
            raise BusinessRulesError("Business rule version was not initialized.")
        return self._version

    def find_relevant_rules(self, question: str) -> List[Dict[str, Any]]:
        """
        Returns every SQL rule whose synonyms appear as whole words in the
        question.
        """
        self._ensure_loaded()

        snapshot = _ACTIVE_RULEBOOK_SNAPSHOT.get()
        if snapshot is not None and snapshot[0] is self:
            terms, ontology_entities = snapshot[2], snapshot[3]
        else:
            terms, ontology_entities = self._terms, self._ontology_entities

        matched: List[Dict[str, Any]] = []

        for term in terms:
            synonyms = term.get("synonyms", [])
            if any(
                _contains_rule_phrase(question, phrase)
                for phrase in term.get("exclude_if_contains", [])
            ):
                continue
            if any(_contains_rule_phrase(question, synonym) for synonym in synonyms):
                matched.append(json.loads(json.dumps(term)))

        for entity in ontology_entities:
            if any(
                _contains_rule_phrase(question, synonym)
                for synonym in entity["synonyms"]
            ):
                matched.append({
                    "term": f"ontology_{entity['entity']}",
                    "synonyms": list(entity["synonyms"]),
                    "definition": entity["definition"],
                    "entity_resolution": json.loads(json.dumps(entity)),
                })

        logger.info(
            "Matched %d business rule(s) for question: %r -> %s",
            len(matched), question, [t["term"] for t in matched],
        )
        return matched

_store: Optional[BusinessRulesStore] = None


def _walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def get_business_rules_store() -> BusinessRulesStore:
    global _store
    if _store is None:
        _store = BusinessRulesStore()
    return _store


def find_relevant_rules(question: str) -> List[Dict[str, Any]]:
    """Convenience function — the main entry point Node 6 calls."""
    return get_business_rules_store().find_relevant_rules(question)