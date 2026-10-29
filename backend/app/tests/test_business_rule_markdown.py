import json
from pathlib import Path

import pytest

from app.agent.nodes.business_rules import BusinessRulesError, BusinessRulesStore
from app.services.business_rule_markdown import (
    BusinessRulesMarkdownError,
    parse_business_rules_markdown,
    render_business_rules_markdown,
    validate_business_rule_schema_references,
)


RULEBOOK_PATH = (
    Path(__file__).resolve().parents[1] / "rules" / "business_rules.json"
)


def test_current_rulebook_round_trips_through_markdown_template():
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))

    markdown = render_business_rules_markdown(rulebook)
    parsed = parse_business_rules_markdown(markdown)

    assert parsed == rulebook


def test_markdown_parser_rejects_missing_sections():
    with pytest.raises(BusinessRulesMarkdownError, match="Missing required sections"):
        parse_business_rules_markdown(
            "# Business Rules\n\n## Terms\n\n```json\n[]\n```"
        )


def test_markdown_parser_rejects_duplicate_sections():
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    markdown = render_business_rules_markdown(rulebook)

    with pytest.raises(BusinessRulesMarkdownError, match="may appear only once"):
        parse_business_rules_markdown(markdown + "\n## Terms\n\n```json\n[]\n```\n")


def test_markdown_parser_rejects_unsupported_sections():
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    markdown = render_business_rules_markdown(rulebook).replace(
        "## Terms", "## Extra Section\n\n```json\n{}\n```\n\n## Terms"
    )

    with pytest.raises(BusinessRulesMarkdownError, match="Unsupported section"):
        parse_business_rules_markdown(markdown)


def test_markdown_parser_rejects_duplicate_json_keys():
    markdown = """# Business Rules

## Metadata

```json
{"database": "MySQL", "version": "1", "version": "2"}
```

## Ontology Entities

```json
[]
```

## Terms

```json
[]
```
"""

    with pytest.raises(BusinessRulesMarkdownError, match="Duplicate JSON key"):
        parse_business_rules_markdown(markdown)


def test_markdown_parser_rejects_invalid_rule_contract():
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    rulebook["terms"][0]["search_scope"] = "arbitrary_table"
    markdown = render_business_rules_markdown(rulebook)

    with pytest.raises(BusinessRulesMarkdownError, match="Invalid search scope"):
        parse_business_rules_markdown(markdown)


def test_business_rules_store_keeps_active_snapshot_if_validation_fails():
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    original_version = store.version
    invalid_rulebook = store.export_rulebook()
    invalid_rulebook["terms"][0]["search_scope"] = "arbitrary_table"

    with pytest.raises(BusinessRulesError, match="Invalid search scope"):
        store.load_data(invalid_rulebook)


@pytest.mark.parametrize(
    ("table", "column", "message"),
    [
        ("missing_table", "id", "table outside"),
        ("approved_table", "unknown_column", "column outside"),
    ],
)
def test_schema_reference_validator_rejects_unknown_tables_and_columns(
    table, column, message
):
    rulebook = {
        "_meta": {"database": "MySQL", "version": "test"},
        "ontology_entities": [],
        "terms": [
            {
                "term": "verified_lookup",
                "synonyms": ["verified lookup"],
                "definition": "Look up a verified record.",
                "query_logic": {
                    "where_expression": {
                        "table": table,
                        "column": column,
                        "operator": "=",
                        "value": 1,
                    }
                },
            }
        ],
    }
    schema = {
        "approved_table": [
            {"column": "id", "data_type": "int", "key": "PRI"},
        ]
    }

    with pytest.raises(BusinessRulesMarkdownError, match=message):
        validate_business_rule_schema_references(rulebook, schema, [])


def test_schema_reference_validator_rejects_unverified_related_entity_edges():
    rulebook = {
        "_meta": {"database": "MySQL", "version": "test"},
        "ontology_entities": [
            {
                "entity": "approved",
                "canonical_table": "approved_table",
                "related_entities": [
                    {
                        "table": "related_table",
                        "relationship": "fk_wrong_edge",
                        "required": True,
                    }
                ],
            }
        ],
        "terms": [],
    }
    schema = {
        "approved_table": [{"column": "id"}],
        "related_table": [{"column": "id"}],
        "unrelated_table": [{"column": "id"}],
    }
    relationships = [
        {
            "constraint_name": "fk_wrong_edge",
            "table": "approved_table",
            "referenced_table": "unrelated_table",
        }
    ]

    with pytest.raises(BusinessRulesMarkdownError, match="unverified relationship"):
        validate_business_rule_schema_references(rulebook, schema, relationships)


def test_schema_reference_validator_rejects_required_related_entity_without_path():
    rulebook = {
        "_meta": {"database": "MySQL", "version": "test"},
        "ontology_entities": [
            {
                "entity": "approved",
                "canonical_table": "approved_table",
                "related_entities": [
                    {"table": "related_table", "required": True}
                ],
            }
        ],
        "terms": [],
    }
    schema = {
        "approved_table": [{"column": "id"}],
        "related_table": [{"column": "id"}],
    }

    with pytest.raises(BusinessRulesMarkdownError, match="no verified relationship path"):
        validate_business_rule_schema_references(rulebook, schema, [])


def test_request_snapshot_pins_rules_and_context_version_during_publish():
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    rulebook["terms"][0]["synonyms"] = ["snapshot sentinel old"]
    store = BusinessRulesStore()
    store.load_data(rulebook)
    old_version = store.version

    next_rulebook = store.export_rulebook()
    next_rulebook["terms"][0]["synonyms"] = ["snapshot sentinel new"]
    with store.request_snapshot():
        store.load_data(next_rulebook)

        assert store.version == old_version
        assert store.find_relevant_rules("snapshot sentinel old")
        assert not store.find_relevant_rules("snapshot sentinel new")

    assert store.version != old_version
    assert store.find_relevant_rules("snapshot sentinel new")
