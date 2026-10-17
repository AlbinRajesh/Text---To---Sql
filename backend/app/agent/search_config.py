"""Fixed, schema-validated fields available to the typed text-search operation."""

import re
from typing import Any, Dict, List


SEARCH_ROOT_TABLES = {"shops": "shop_details"}

SEARCHABLE_COLUMNS: Dict[str, tuple[Dict[str, Any], ...]] = {
    "shops": (
        {
            "table": "shop_details",
            "column": "name",
            "relationship": None,
        },
        {
            "table": "offer",
            "column": "offer_heading",
            "relationship": {
                "constraint_name": "reviewed_offer_shop_id",
                "table": "offer",
                "referenced_table": "shop_details",
            },
        },
        {
            "table": "subcategories",
            "column": "subcategoryname",
            "relationship": {
                "constraint_name": "reviewed_shop_details_subcategory_id",
                "table": "shop_details",
                "referenced_table": "subcategories",
            },
        },
    ),
}

# The service field stays deliberately disabled until its FK is verified and
# added to the relationship catalog. Disabled fields are never compiled.
DISABLED_SEARCHABLE_COLUMNS = (
    {
        "scope": "shops",
        "table": "service",
        "column": "service_name",
        "enabled": False,
        "reason": "offer.service_id -> service.id is not yet verified",
    },
)

MAX_SEARCH_TERM_LENGTH = 100
MAX_SHOP_NAME_WORDS = 5
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEXT_TYPES = {
    "char",
    "varchar",
    "tinytext",
    "text",
    "mediumtext",
    "longtext",
}


class SearchConfigurationError(ValueError):
    """Raised when a configured search field is not backed by live schema."""


def normalize_shop_name_term(term: str) -> str:
    """Normalize a user term without letting it exceed the configured bound."""
    return term.strip().casefold()[:MAX_SEARCH_TERM_LENGTH].strip()


def shop_name_search_words(term: str, *, like_pattern: bool = False) -> List[str]:
    """Return the bounded, unique words used by shop-name matching."""
    source = term.replace("%", " ") if like_pattern else term
    words = []
    seen = set()
    for word in source.split():
        key = word.casefold()
        if len(word) >= 2 and key not in seen:
            seen.add(key)
            words.append(word)
    return words[:MAX_SHOP_NAME_WORDS]


def escape_shop_name_like_word(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def validate_searchable_columns(
    schema_context: Dict[str, List[Dict[str, Any]]],
    relationships: List[Dict[str, Any]],
) -> None:
    """Fail startup if an enabled search field or its verified join is invalid."""
    if not isinstance(SEARCHABLE_COLUMNS, dict) or not SEARCHABLE_COLUMNS:
        raise SearchConfigurationError("Search scopes must be a non-empty mapping.")

    relationship_index = {
        (
            relationship.get("constraint_name"),
            relationship.get("table"),
            relationship.get("referenced_table"),
        ): relationship
        for relationship in relationships
        if isinstance(relationship, dict)
    }
    seen_fields = set()

    for scope, fields in SEARCHABLE_COLUMNS.items():
        if not isinstance(scope, str) or not _IDENTIFIER.fullmatch(scope):
            raise SearchConfigurationError(f"Invalid search scope {scope!r}.")
        root_table = SEARCH_ROOT_TABLES.get(scope)
        if root_table is None or root_table not in schema_context:
            raise SearchConfigurationError(
                f"Search scope {scope!r} has no available root table."
            )
        if not isinstance(fields, (tuple, list)) or not fields:
            raise SearchConfigurationError(
                f"Search scope {scope!r} must contain at least one field."
            )

        for field in fields:
            if not isinstance(field, dict) or set(field) != {
                "table", "column", "relationship"
            }:
                raise SearchConfigurationError(
                    f"Invalid searchable-column entry in scope {scope!r}."
                )
            table = field["table"]
            column = field["column"]
            if (
                not isinstance(table, str)
                or not _IDENTIFIER.fullmatch(table)
                or not isinstance(column, str)
                or not _IDENTIFIER.fullmatch(column)
            ):
                raise SearchConfigurationError(
                    f"Invalid searchable identifier {table!r}.{column!r}."
                )
            identity = (scope, table, column)
            if identity in seen_fields:
                raise SearchConfigurationError(
                    f"Duplicate searchable field {table}.{column} in {scope!r}."
                )
            seen_fields.add(identity)
            column_info = next(
                (
                    entry
                    for entry in schema_context.get(table, [])
                    if isinstance(entry, dict) and entry.get("column") == column
                ),
                None,
            )
            if column_info is None:
                raise SearchConfigurationError(
                    f"Searchable column {table}.{column} is absent from the live schema."
                )
            data_type = column_info.get("data_type")
            if not isinstance(data_type, str) or data_type.casefold() not in _TEXT_TYPES:
                raise SearchConfigurationError(
                    f"Searchable column {table}.{column} must have a text type."
                )

            selected_relationship = field["relationship"]
            if table == root_table:
                if selected_relationship is not None:
                    raise SearchConfigurationError(
                        f"Root search field {table}.{column} must not declare a join."
                    )
                continue
            if not isinstance(selected_relationship, dict) or set(
                selected_relationship
            ) != {"constraint_name", "table", "referenced_table"}:
                raise SearchConfigurationError(
                    f"Searchable column {table}.{column} needs one verified "
                    "relationship to its search root."
                )
            relation_key = (
                selected_relationship["constraint_name"],
                selected_relationship["table"],
                selected_relationship["referenced_table"],
            )
            relationship = relationship_index.get(relation_key)
            if relationship is None:
                raise SearchConfigurationError(
                    f"Searchable column {table}.{column} references an "
                    "unverified relationship."
                )
            if {relationship["table"], relationship["referenced_table"]} != {
                table, root_table
            }:
                raise SearchConfigurationError(
                    f"Relationship for {table}.{column} does not connect to "
                    f"search root {root_table}."
                )
            if not relationship.get("column_pairs"):
                raise SearchConfigurationError(
                    f"Relationship for {table}.{column} has no join columns."
                )

    for disabled in DISABLED_SEARCHABLE_COLUMNS:
        if (
            not isinstance(disabled, dict)
            or disabled.get("enabled") is not False
            or disabled.get("scope") not in SEARCH_ROOT_TABLES
            or not isinstance(disabled.get("table"), str)
            or not _IDENTIFIER.fullmatch(disabled["table"])
            or not isinstance(disabled.get("column"), str)
            or not _IDENTIFIER.fullmatch(disabled["column"])
            or not isinstance(disabled.get("reason"), str)
            or not disabled["reason"].strip()
        ):
            raise SearchConfigurationError(
                "Disabled searchable-column entries must be explicit and well-formed."
            )


def search_term_variants(term: str) -> List[str]:
    """Return the literal term and a Latin plural-stripped form when it differs."""
    def strip_plural(match: re.Match[str]) -> str:
        word = match.group(0)
        if len(word) <= 3:
            return word
        folded = word.casefold()
        if folded.endswith(("xes", "zes", "ches", "shes")):
            return word[:-2]
        if folded.endswith("s"):
            return word[:-1]
        return word

    singular = re.sub(r"[A-Za-z]+", strip_plural, term)
    return list(dict.fromkeys((term, singular)))


def escape_like_term(term: str) -> str:
    """Escape the chosen LIKE escape character and user-supplied wildcards."""
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
