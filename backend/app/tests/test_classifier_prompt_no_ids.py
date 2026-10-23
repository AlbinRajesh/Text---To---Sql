"""The classifier prompt must not hard-code category or subcategory IDs."""

import json
import re

import app.agent.prompts.classifier_prompt as classifier_prompt

ID_PATTERN = re.compile(r"\b(?:sub)?category_id\s*(?:is|=)?\s*\d+", re.IGNORECASE)


def test_classifier_prompt_has_no_hard_coded_ids():
    texts = [v for v in vars(classifier_prompt).values() if isinstance(v, str)]
    assert texts
    assert not any(ID_PATTERN.search(text) for text in texts)


def test_classifier_prompt_defines_complete_temporal_and_shop_type_shapes():
    prompt = classifier_prompt.SYSTEM_PROMPT

    assert '"current_year"' in prompt
    assert '"target":"bookings"' in prompt
    assert 'contain only "mention"' in prompt
    assert "do not ask clarification merely because a qualifier is not a catalog type" in prompt


def test_classifier_payload_contains_no_full_value_or_rule_catalogs():
    messages = classifier_prompt.build_classifier_messages(
        "Show restaurants in Nagercoil",
        prior_context=[],
        business_rule_terms=["active_shop_status", "booking_amount"],
    )

    payload = json.loads(messages[1]["content"])
    assert "business_value_catalog" not in payload
    assert "business_rule_catalog" not in payload
    assert payload["business_rule_terms"] == [
        "active_shop_status",
        "booking_amount",
    ]