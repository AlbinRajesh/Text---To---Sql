"""The classifier prompt must not hard-code category or subcategory IDs."""

import re

import app.agent.prompts.classifier_prompt as classifier_prompt

ID_PATTERN = re.compile(r"\b(?:sub)?category_id\s*(?:is|=)?\s*\d+", re.IGNORECASE)


def test_classifier_prompt_has_no_hard_coded_ids():
    texts = [v for v in vars(classifier_prompt).values() if isinstance(v, str)]
    assert texts
    assert not any(ID_PATTERN.search(text) for text in texts)