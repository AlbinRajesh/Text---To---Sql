import json
from pathlib import Path

import pytest

from app.agent.nodes.business_rules import BusinessRulesError, BusinessRulesStore


RULEBOOK_PATH = (
    Path(__file__).resolve().parents[1] / "rules" / "business_rules.json"
)


def test_mysql_semantic_rulebook_loads():
    store = BusinessRulesStore(str(RULEBOOK_PATH))

    assert store.version
    assert store.find_relevant_rules(
        "How many shops have an average rating above 4.5?"
    )[0]["term"] == "shops_above_average_rating_threshold"


def test_semantic_contract_rejects_invalid_threshold_pattern(tmp_path):
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    rulebook["terms"][0]["query_logic"]["semantic_contract"][
        "threshold_pattern"
    ] = r"\babove\s+(\d+(?:\.\d+)?"
    path = tmp_path / "business_rules.json"
    path.write_text(json.dumps(rulebook), encoding="utf-8")
    store = BusinessRulesStore(str(path))

    with pytest.raises(BusinessRulesError, match="Invalid threshold pattern"):
        store.find_relevant_rules("How many shops have an average rating above 4.5?")
