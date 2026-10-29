"""The classifier prompt must not hard-code category or subcategory IDs."""

import json
import re

import app.agent.prompts.classifier_prompt as classifier_prompt
from app.agent.prompts.classifier_prompt import build_classifier_messages

ID_PATTERN = re.compile(r"\b(?:sub)?category_id\s*(?:is|=)?\s*\d+", re.IGNORECASE)


def test_classifier_prompt_has_no_hard_coded_ids():
    texts = [v for v in vars(classifier_prompt).values() if isinstance(v, str)]
    assert texts
    assert not any(ID_PATTERN.search(text) for text in texts)


def test_classifier_prompt_guides_clear_implicit_metric_followups():
    prompt = classifier_prompt.SYSTEM_PROMPT.casefold()
    assert "implicit follow-ups" in prompt
    assert "preserve the prior metric and grouping" in prompt
    assert "clearly standalone or unrelated" in prompt
    assert "multiple prior measures" in prompt


def test_classifier_prompt_routes_where_to_get_item_as_name_search():
    prompt = classifier_prompt.SYSTEM_PROMPT.casefold()

    assert "treat the item as a literal shop-name search" in prompt
    assert "verify inventory or that the shops sell it" in prompt
    assert "where can i get a juice in nagercoil?" in prompt


def test_classifier_receives_saved_ranking_context():
    context = [{
        "original_question": "Show booking count by category",
        "metrics": [{
            "table": "bookings",
            "column": "id",
            "aggregate": "COUNT",
            "alias": "booking_count",
        }],
        "query": {
            "table": "bookings",
            "group_by": [{"table": "categories", "column": "categoriesname"}],
            "ranking_intent": "none",
            "order_by": [],
            "limit": None,
        },
    }]

    messages = build_classifier_messages(
        "Which category had the highest count?",
        prior_context=context,
    )
    payload = json.loads(messages[1]["content"])

    assert payload["current_question"] == "Which category had the highest count?"
    assert payload["prior_verified_results"] == context
