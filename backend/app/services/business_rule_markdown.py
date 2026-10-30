"""Strict Markdown envelope for complete business-rule snapshots."""

import json
import re
from typing import Any, Dict

from app.agent.nodes.business_rules import BusinessRulesError, BusinessRulesStore

MAX_MARKDOWN_BYTES = 256 * 1024
_SECTION_PATTERN = re.compile(
    r"(?m)^## (.+?)\s*$"
)
_FENCE_PATTERN = re.compile(r"```json\s*\r?\n(.*?)\r?\n```", re.DOTALL)
_SECTION_NAMES = {
    "Metadata": "_meta",
    "Ontology Entities": "ontology_entities",
    "Terms": "terms",
}


class BusinessRulesMarkdownError(ValueError):
    """Raised when an uploaded rulebook is not in the supported Markdown format."""


def parse_business_rules_markdown(markdown: str) -> Dict[str, Any]:
    """Parse and validate a full rulebook represented by three JSON code blocks."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise BusinessRulesMarkdownError("The Markdown document must not be empty.")
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise BusinessRulesMarkdownError(
            f"The Markdown document exceeds {MAX_MARKDOWN_BYTES} bytes."
        )

    sections = list(_SECTION_PATTERN.finditer(markdown))
    parsed: Dict[str, Any] = {}
    seen = set()
    for index, match in enumerate(sections):
        title = match.group(1)
        if title not in _SECTION_NAMES:
            raise BusinessRulesMarkdownError(
                f"Unsupported section '## {title}'."
            )
        if title in seen:
            raise BusinessRulesMarkdownError(
                f"The section '## {title}' may appear only once."
            )
        seen.add(title)
        end = sections[index + 1].start() if index + 1 < len(sections) else len(markdown)
        section_body = markdown[match.end():end]
        blocks = list(_FENCE_PATTERN.finditer(section_body))
        if len(blocks) != 1:
            raise BusinessRulesMarkdownError(
                f"The section '## {title}' must contain exactly one ```json block."
            )
        if section_body[:blocks[0].start()].strip() or section_body[blocks[0].end():].strip():
            raise BusinessRulesMarkdownError(
                f"The section '## {title}' may contain only its JSON block."
            )
        try:
            parsed[_SECTION_NAMES[title]] = json.loads(
                blocks[0].group(1),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except BusinessRulesMarkdownError:
            raise
        except json.JSONDecodeError as e:
            raise BusinessRulesMarkdownError(
                f"Invalid JSON in section '## {title}' at line {e.lineno}, "
                f"column {e.colno}: {e.msg}"
            ) from e
        except ValueError as e:
            raise BusinessRulesMarkdownError(
                f"Invalid JSON in section '## {title}': {e}"
            ) from e

    missing = set(_SECTION_NAMES) - seen
    if missing:
        raise BusinessRulesMarkdownError(
            "Missing required sections: "
            + ", ".join(f"## {section}" for section in sorted(missing))
            + "."
        )

    rulebook = {
        "_meta": parsed["_meta"],
        "ontology_entities": parsed["ontology_entities"],
        "terms": parsed["terms"],
    }
    try:
        BusinessRulesStore().load_data(rulebook)
    except BusinessRulesError as e:
        raise BusinessRulesMarkdownError(str(e)) from e
    return rulebook


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BusinessRulesMarkdownError(
                f"Duplicate JSON key {key!r} is not allowed."
            )
        result[key] = value
    return result


def validate_business_rule_schema_references(
    rulebook: Dict[str, Any],
    schema: Dict[str, list[dict]],
    relationships: list[dict],
) -> None:
    """Reject explicit rule references absent from the verified live schema."""
    if not schema:
        raise BusinessRulesMarkdownError(
            "The live schema is unavailable; rule references cannot be validated."
        )
    relationship_by_name = {
        item.get("constraint_name"): item
        for item in relationships
        if isinstance(item, dict) and isinstance(item.get("constraint_name"), str)
    }
    neighbors: Dict[str, set[str]] = {}
    for relationship in relationship_by_name.values():
        left = relationship.get("table")
        right = relationship.get("referenced_table")
        if isinstance(left, str) and isinstance(right, str):
            neighbors.setdefault(left, set()).add(right)
            neighbors.setdefault(right, set()).add(left)

    def require_table(table: Any, location: str) -> str:
        if not isinstance(table, str) or table not in schema:
            raise BusinessRulesMarkdownError(
                f"{location} references a table outside the verified schema."
            )
        return table

    def require_column(table: Any, column: Any, location: str) -> None:
        table_name = require_table(table, location)
        if not isinstance(column, str) or not any(
            isinstance(item, dict) and item.get("column") == column
            for item in schema[table_name]
        ):
            raise BusinessRulesMarkdownError(
                f"{location} references a column outside the verified schema."
            )

    def has_path(start: str, goal: str) -> bool:
        pending = [start]
        seen = {start}
        while pending:
            current = pending.pop()
            if current == goal:
                return True
            for neighbor in neighbors.get(current, set()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        return False

    def validate_entity_resolution(entity: dict, location: str) -> None:
        canonical_table = require_table(
            entity.get("canonical_table"),
            f"{location}.canonical_table",
        )
        temporal_column = entity.get("temporal_column")
        if temporal_column:
            require_column(
                canonical_table,
                temporal_column,
                f"{location}.temporal_column",
            )
        for index, item in enumerate(entity.get("filters", [])):
            table = item.get("table", canonical_table)
            require_column(table, item.get("column"), f"{location}.filters[{index}]")
        for index, item in enumerate(entity.get("related_entities", [])):
            related_table = require_table(
                item.get("table"),
                f"{location}.related_entities[{index}].table",
            )
            relationship_name = item.get("relationship")
            if relationship_name is not None:
                relationship = relationship_by_name.get(relationship_name)
                if relationship is None or {
                    relationship.get("table"),
                    relationship.get("referenced_table"),
                } != {canonical_table, related_table}:
                    raise BusinessRulesMarkdownError(
                        f"{location}.related_entities[{index}] references an "
                        "unverified relationship."
                    )
            elif item.get("required") is True and not has_path(
                canonical_table, related_table
            ):
                raise BusinessRulesMarkdownError(
                    f"{location}.related_entities[{index}] has no verified "
                    "relationship path."
                )
        exclusions = entity.get("exclude_entities_with_rows_in", [])
        if not isinstance(exclusions, list):
            raise BusinessRulesMarkdownError(
                f"{location}.exclude_entities_with_rows_in must be an array."
            )
        for index, exclusion in enumerate(exclusions):
            exclusion_location = (
                f"{location}.exclude_entities_with_rows_in[{index}]"
            )
            if (
                not isinstance(exclusion, dict)
                or set(exclusion)
                != {"table", "relationship", "where_expression"}
            ):
                raise BusinessRulesMarkdownError(
                    f"{exclusion_location} has an invalid shape."
                )
            child_table = require_table(
                exclusion.get("table"),
                f"{exclusion_location}.table",
            )
            relationship_spec = exclusion.get("relationship")
            if (
                child_table == canonical_table
                or not isinstance(relationship_spec, dict)
                or set(relationship_spec)
                != {"constraint_name", "table", "referenced_table"}
                or relationship_spec.get("table") != child_table
                or relationship_spec.get("referenced_table") != canonical_table
            ):
                raise BusinessRulesMarkdownError(
                    f"{exclusion_location} must identify a child-to-base relationship."
                )
            relationship = relationship_by_name.get(
                relationship_spec["constraint_name"]
            )
            if (
                relationship is None
                or relationship.get("table") != child_table
                or relationship.get("referenced_table") != canonical_table
                or relationship.get("cardinality") != "many_to_one"
                or relationship.get("verification")
                not in {"declared_foreign_key", "reviewed_manual_query"}
            ):
                raise BusinessRulesMarkdownError(
                    f"{exclusion_location} references an unverified "
                    "many-to-one relationship."
                )
            parent_primary_keys = {
                item.get("column")
                for item in schema.get(canonical_table, [])
                if isinstance(item, dict) and item.get("key") == "PRI"
            }
            if not relationship.get("column_pairs") or any(
                pair.get("referenced_column") not in parent_primary_keys
                or not any(
                    isinstance(item, dict)
                    and item.get("column") == pair.get("column")
                    for item in schema.get(child_table, [])
                )
                for pair in relationship["column_pairs"]
            ):
                raise BusinessRulesMarkdownError(
                    f"{exclusion_location} does not correlate to the base "
                    "table's verified primary key."
                )

            def validate_exclusion_filter(value: Any, filter_location: str) -> None:
                if value is None:
                    return
                if not isinstance(value, dict):
                    raise BusinessRulesMarkdownError(
                        f"{filter_location} must be a filter object."
                    )
                if "boolean" in value:
                    if (
                        set(value) != {"boolean", "conditions"}
                        or value.get("boolean") not in {"AND", "OR"}
                        or not isinstance(value.get("conditions"), list)
                        or not value["conditions"]
                    ):
                        raise BusinessRulesMarkdownError(
                            f"{filter_location} has an invalid boolean filter."
                        )
                    for child_index, child in enumerate(value["conditions"]):
                        validate_exclusion_filter(
                            child, f"{filter_location}.conditions[{child_index}]"
                        )
                    return
                if set(value) - {"table", "column", "operator", "value"}:
                    raise BusinessRulesMarkdownError(
                        f"{filter_location} contains unsupported filter fields."
                    )
                owner = value.get("table", child_table)
                if owner != child_table:
                    raise BusinessRulesMarkdownError(
                        f"{filter_location} must filter only its excluded child table."
                    )
                require_column(owner, value.get("column"), filter_location)
                if not isinstance(value.get("operator"), str):
                    raise BusinessRulesMarkdownError(
                        f"{filter_location} must specify a filter operator."
                    )

            validate_exclusion_filter(
                exclusion.get("where_expression"),
                f"{exclusion_location}.where_expression",
            )

    for index, entity in enumerate(rulebook.get("ontology_entities", [])):
        validate_entity_resolution(entity, f"ontology_entities[{index}]")

    def validate_logic(value: Any, location: str) -> None:
        if isinstance(value, dict):
            table = value.get("table")
            column = value.get("column")
            if table is not None and column is not None:
                require_column(table, column, location)
            for key, child in value.items():
                validate_logic(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                validate_logic(child, f"{location}[{index}]")

    for index, term in enumerate(rulebook.get("terms", [])):
        location = f"terms[{index}]"
        entity_resolution = term.get("entity_resolution")
        if isinstance(entity_resolution, dict):
            validate_entity_resolution(
                entity_resolution,
                f"{location}.entity_resolution",
            )

        logic = term.get("query_logic", {})
        validate_logic(logic, f"{location}.query_logic")
        contract = logic.get("semantic_contract")
        if isinstance(contract, dict):
            source_table = require_table(
                contract.get("source_table"),
                f"{location}.query_logic.semantic_contract.source_table",
            )
            entity_table = require_table(
                contract.get("entity_table"),
                f"{location}.query_logic.semantic_contract.entity_table",
            )
            relationship = relationship_by_name.get(contract.get("relationship"))
            if relationship is None or {
                relationship.get("table"),
                relationship.get("referenced_table"),
            } != {source_table, entity_table}:
                raise BusinessRulesMarkdownError(
                    f"{location}.query_logic.semantic_contract references an "
                    "unverified relationship."
                )
            for field in ("relationship_column", "measure_column", "count_column"):
                if contract.get(field):
                    require_column(
                        source_table,
                        contract[field],
                        f"{location}.query_logic.semantic_contract.{field}",
                    )
            for field in ("entity_key", "entity_label_column"):
                if contract.get(field):
                    require_column(
                        entity_table,
                        contract[field],
                        f"{location}.query_logic.semantic_contract.{field}",
                    )

    return None


def render_business_rules_markdown(rulebook: Dict[str, Any]) -> str:
    """Render a full rulebook using the deterministic editable template."""
    sections = (
        ("Metadata", rulebook.get("_meta")),
        ("Ontology Entities", rulebook.get("ontology_entities")),
        ("Terms", rulebook.get("terms")),
    )
    rendered = [
        "# Business Rules",
        "",
        "This document replaces the complete rulebook. Preserve the three section ",
        "headings and one `json` block per section. The backend validates every field",
        "before saving a draft and only changes active rules after explicit publish.",
        "",
    ]
    for title, value in sections:
        rendered.extend(
            [
                f"## {title}",
                "",
                "```json",
                json.dumps(value, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(rendered)
