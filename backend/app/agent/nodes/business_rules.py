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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RULES_FILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "rules", "business_rules.json"
)

def _build_word_pattern(term: str) -> re.Pattern:
    """Matches whole words only, so "active" doesn't match inside "proactive"."""
    return re.compile(r"\b" + re.escape(term.lower()) + r"\b")


class BusinessRulesError(Exception):
    """Raised when the rulebook file is missing, unreadable, or malformed."""
    pass


class BusinessRulesStore:
    """
    Loads business_rules.json once and keeps it cached in memory.
    Call reload() explicitly if the rulebook is updated while the app is running.
    """

    def __init__(self, file_path: str = RULES_FILE_PATH) -> None:
        self._file_path = file_path
        self._terms: List[Dict[str, Any]] = []
        self._loaded = False
        self._version: Optional[str] = None

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

        validated_terms = []
        seen_terms = set()
        for entry in terms:
            if not isinstance(entry, dict):
                raise BusinessRulesError("Each business rule must be a JSON object.")
            term = entry.get("term")
            synonyms = entry.get("synonyms")
            definition = entry.get("definition")
            anchor_tables = entry.get("anchor_tables")
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
                or (
                    anchor_tables is not None
                    and (
                        not isinstance(anchor_tables, list)
                        or not anchor_tables
                        or any(
                            not isinstance(table, str) or not table.strip()
                            for table in anchor_tables
                        )
                    )
                )
                or not isinstance(logic, dict)
                or set(logic) - {
                    "where_expression",
                    "having_expression",
                    "semantic_contract",
                    "state_filter",
                    "geographic_alias",
                }
            ):
                raise BusinessRulesError(
                    f"Invalid or unsupported SQL business rule: {term!r}."
                )
            contract = logic.get("semantic_contract")
            state_filter = logic.get("state_filter")
            if state_filter is not None and (
                not isinstance(state_filter, dict)
                or set(state_filter) != {
                    "entity", "state", "table", "column", "value",
                }
                or any(
                    not isinstance(state_filter.get(key), str)
                    or not state_filter[key].strip()
                    for key in ("entity", "state", "table", "column")
                )
                or isinstance(state_filter.get("value"), bool)
                or not isinstance(state_filter.get("value"), (int, str))
            ):
                raise BusinessRulesError(
                    f"Invalid state filter for SQL business rule {term!r}."
                )
            geographic_alias = logic.get("geographic_alias")
            if geographic_alias is not None:
                if (
                    not isinstance(geographic_alias, dict)
                    or set(geographic_alias) != {"table", "column", "groups"}
                    or any(
                        not isinstance(geographic_alias.get(key), str)
                        or not geographic_alias[key].strip()
                        for key in ("table", "column")
                    )
                    or not isinstance(geographic_alias.get("groups"), list)
                    or not geographic_alias["groups"]
                ):
                    raise BusinessRulesError(
                        f"Invalid geographic alias for SQL business rule {term!r}."
                    )
                for group in geographic_alias["groups"]:
                    if (
                        not isinstance(group, dict)
                        or set(group) != {"aliases", "values"}
                        or not isinstance(group.get("aliases"), list)
                        or not group["aliases"]
                        or not isinstance(group.get("values"), list)
                        or not group["values"]
                        or any(
                            not isinstance(value, str) or not value.strip()
                            for value in group["aliases"] + group["values"]
                        )
                    ):
                        raise BusinessRulesError(
                            f"Invalid geographic alias group for SQL business rule {term!r}."
                        )
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
            seen_terms.add(term)
            validated_terms.append(entry)

        self._terms = validated_terms
        self._version = hashlib.sha256(raw).hexdigest()
        self._loaded = True
        logger.info("Loaded %d business rule term(s) from %s", len(terms), self._file_path)

    def reload(self) -> None:
        """Forces a re-read of the rulebook from disk."""
        self._load()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    @property
    def version(self) -> str:
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

        question_lower = question.lower()
        matched: List[Dict[str, Any]] = []

        for term in self._terms:
            synonyms = term.get("synonyms", [])
            if any(_build_word_pattern(s).search(question_lower) for s in synonyms):
                matched.append(json.loads(json.dumps(term)))

        logger.info(
            "Matched %d business rule(s) for question: %r -> %s",
            len(matched), question, [t["term"] for t in matched],
        )
        return matched

    def get_rule_catalog(self) -> List[Dict[str, Any]]:
        """Return canonical business-rule identifiers and definitions for AI selection."""
        self._ensure_loaded()
        catalog = []
        for entry in self._terms:
            item = {"term": entry["term"], "definition": entry["definition"]}
            state_filter = entry.get("query_logic", {}).get("state_filter")
            if state_filter is not None:
                item["state_filter"] = state_filter
            catalog.append(item)
        return catalog

    def find_rules_by_terms(self, terms: List[str]) -> List[Dict[str, Any]]:
        """Resolve structured rule identifiers against the validated rulebook."""
        self._ensure_loaded()
        if not isinstance(terms, list) or any(
            not isinstance(term, str) or not term.strip() for term in terms
        ):
            raise BusinessRulesError("Structured business-rule terms must be non-empty strings.")

        rules_by_term = {entry["term"]: entry for entry in self._terms}
        unknown_terms = sorted(set(terms) - rules_by_term.keys())
        if unknown_terms:
            raise BusinessRulesError(
                f"Classifier selected unknown business-rule terms: {unknown_terms}."
            )
        return [
            json.loads(json.dumps(rules_by_term[term]))
            for term in dict.fromkeys(terms)
        ]

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


def get_business_rule_catalog() -> List[Dict[str, Any]]:
    return get_business_rules_store().get_rule_catalog()


def find_rules_by_terms(terms: List[str]) -> List[Dict[str, Any]]:
    return get_business_rules_store().find_rules_by_terms(terms)