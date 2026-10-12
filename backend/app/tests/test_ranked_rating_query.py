import asyncio

import pytest

from app.agent.nodes import generate_query as gq
from app.agent.graph import _pick_tie_column
from app.agent.nodes.safety_check import run_safety_check
from app.agent.prompts.generate_query_prompt import RATING_RANKING_POLICY, SYSTEM_PROMPT
from app.agent.prompts.grader_prompt import build_grader_prompt
SCHEMA = {
    "reviews": [
        {"column": "id", "data_type": "bigint", "key": "PRI"},
        {"column": "shop_id", "data_type": "bigint", "key": ""},
        {"column": "rating", "data_type": "float", "key": ""},
    ],
    "shop_details": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "name", "data_type": "text", "key": ""},
    ],
}
RELATIONSHIPS = [{
    "constraint_name": "reviewed_reviews_shop_id",
    "table": "reviews",
    "referenced_table": "shop_details",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
}]


KNOWN_CITIES = [
    "Kazhakkoottam", "Thiruvananthapuram", "Nagercoil", "Nahercoil",
    "Kanyakumari", "Kanniyakumari", "Ganapathipuram", "ganabathipuram",
    "Konam", "North Konam", "Chennai",
]


def _plan(monkeypatch, question, ids, cities=KNOWN_CITIES):
    calls = []

    async def fake_fetch(sql, params=()):
        calls.append((sql, params))
        return [(i,) for i in ids]

    async def fake_cities():
        return list(cities)

    monkeypatch.setattr(gq, "fetch_all_readonly", fake_fetch)
    monkeypatch.setattr(gq, "_load_known_cities", fake_cities)
    plan = asyncio.run(
        gq._build_ranked_rating_query((question, ""), SCHEMA, RELATIONSHIPS)
    )
    return plan, calls

def test_city_extraction():
    assert gq._extract_city("best rated place in Kazhakkoottam") == "Kazhakkoottam"
    assert gq._extract_city(
        "need a car mechanic in Thiruvananthapuram with good reviews"
    ) == "Thiruvananthapuram"
    assert gq._extract_city("best rated shop") is None


def test_not_triggered_without_intent_or_city(monkeypatch):
    plan, calls = _plan(monkeypatch, "list shops in Nagercoil", [1])
    assert plan is None and calls == []
    plan, _ = _plan(monkeypatch, "best rated place", [1])
    assert plan is None


def test_not_triggered_for_counts(monkeypatch):
    plan, _ = _plan(monkeypatch, "how many best rated shops in Nagercoil", [1])
    assert plan is None


def test_plan_uses_found_ids_and_no_address_join(monkeypatch):
    plan, calls = _plan(monkeypatch, "best rated place in Kazhakkoottam", [7, 9])
    assert [j["table"] for j in plan["joins"]] == ["shop_details"]
    in_filter = plan["where_expression"]["conditions"][0]
    assert in_filter["value"] == [7, 9]
    assert calls[0][1] == ("Kazhakkoottam",)
    assert "is_default = 1" in calls[0][0]


def test_best_rated_shop_in_nagercoil_uses_the_ranked_builder(monkeypatch):
    plan, calls = _plan(monkeypatch, "best rated shop in Nagercoil", [3])
    assert plan is not None
    assert [join["table"] for join in plan["joins"]] == ["shop_details"]
    assert calls


@pytest.mark.parametrize(
    "question",
    [
        "What is the full address of the highest-rated shop in Kazhakkoottam?",
        "Show me the products available at the highest-rated restaurant in Nagercoil.",
    ],
)
def test_ranked_builder_steps_aside_for_extra_requested_output(monkeypatch, question):
    plan, calls = _plan(monkeypatch, question, [3])
    assert plan is None
    assert calls == []


def test_mechanic_adds_subcategory_to_lookup(monkeypatch):
    _, calls = _plan(
        monkeypatch, "need a car mechanic in Thiruvananthapuram with good reviews", []
    )
    assert calls[0][1] == ("Thiruvananthapuram", "Bike & Car Servicing")


def test_no_shops_found_returns_zero_row_plan(monkeypatch):
    plan, _ = _plan(
        monkeypatch, "need a car mechanic in Thiruvananthapuram with good reviews", []
    )
    assert plan["where_expression"]["conditions"][0]["value"] == [0]


def test_lookup_failure_becomes_query_generation_error(monkeypatch):
    async def boom(sql, params=()):
        raise OSError("db down")

    monkeypatch.setattr(gq, "fetch_all_readonly", boom)
    with pytest.raises(gq.QueryGenerationError):
        asyncio.run(gq._build_ranked_rating_query(
            ("best rated place in Nagercoil", ""), SCHEMA, RELATIONSHIPS
        ))


def test_plan_passes_validation_and_all_safety_checks(monkeypatch):
    plan, _ = _plan(monkeypatch, "best rated place in Kazhakkoottam", [7, 9])
    plan.pop("grader_note")
    validated = gq._validate_llm_output(
        plan, SCHEMA, RELATIONSHIPS, "best rated place in Kazhakkoottam", []
    )
    result = run_safety_check(validated, list(SCHEMA), SCHEMA, RELATIONSHIPS)
    sql = result["sql"]
    assert "shop_address" not in sql
    assert "AVG(" in sql and "ORDER BY" in sql
    assert "INNER JOIN `shop_details`" in sql


def test_ranked_rating_policy_and_stable_tie_break_are_shared(monkeypatch):
    plan, _ = _plan(monkeypatch, "best rated place in Nagercoil", [3])

    assert RATING_RANKING_POLICY in SYSTEM_PROMPT
    assert RATING_RANKING_POLICY in plan["reasoning"]
    assert plan["order_by"] == [
        {"column": "avg_rating", "direction": "DESC"},
        {"column": "review_count", "direction": "DESC"},
        {"column": "shop_id", "direction": "ASC"},
    ]
    assert _pick_tie_column(plan, ["shop_id", "shop_name", "avg_rating", "review_count"]) == (
        "avg_rating"
    )


def test_grader_note_only_when_shops_found(monkeypatch):
    plan, _ = _plan(monkeypatch, "best rated place in Kazhakkoottam", [7, 9])
    assert "Kazhakkoottam" in plan["grader_note"]
    plan, _ = _plan(monkeypatch, "best rated place in Kazhakkoottam", [])
    assert plan["grader_note"] is None


def test_grader_prompt_includes_note_only_when_given():
    assert "Trusted system note" in build_grader_prompt("q", "r", "sql", "note text")
    assert "note text" not in build_grader_prompt("q", "r", "sql")

def test_lookup_only_returns_active_shops(monkeypatch):
    _, calls = _plan(monkeypatch, "best rated place in Kazhakkoottam", [7])
    assert "sd.status = 1" in calls[0][0]

def _expand(monkeypatch, city, cities=KNOWN_CITIES):
    async def fake_cities():
        return list(cities)

    monkeypatch.setattr(gq, "_load_known_cities", fake_cities)
    return asyncio.run(gq._expand_city_spellings(city))


def test_close_spellings_are_included(monkeypatch):
    assert _expand(monkeypatch, "Nagercoil") == ["Nagercoil", "Nahercoil"]
    assert _expand(monkeypatch, "Kanyakumari") == ["Kanniyakumari", "Kanyakumari"]
    assert _expand(monkeypatch, "Ganapathipuram") == ["Ganapathipuram", "ganabathipuram"]


def test_case_is_ignored(monkeypatch):
    assert _expand(monkeypatch, "chennai") == ["Chennai"]


def test_different_places_are_not_merged(monkeypatch):
    assert _expand(monkeypatch, "Konam") == ["Konam"]


def test_unknown_city_is_used_as_typed(monkeypatch):
    assert _expand(monkeypatch, "Atlantis") == ["Atlantis"]


def test_city_list_failure_falls_back_to_exact_city(monkeypatch):
    async def boom():
        raise OSError("db down")

    monkeypatch.setattr(gq, "_load_known_cities", boom)
    assert asyncio.run(gq._expand_city_spellings("Nagercoil")) == ["Nagercoil"]


def test_lookup_uses_all_spellings(monkeypatch):
    _, calls = _plan(monkeypatch, "best rated place in Nagercoil", [3])
    assert calls[0][1] == ("Nagercoil", "Nahercoil")
    assert "sa.city IN (%s, %s)" in calls[0][0]


def test_note_says_ranked_list_is_intended(monkeypatch):
    plan, _ = _plan(monkeypatch, "best rated place in Kazhakkoottam", [7, 9])
    assert "ranked list of up to 5 shops" in plan["grader_note"]


# ---------------- cheap juice places ----------------
OFFER_SCHEMA = {
    **SCHEMA,
    "offer": [
        {"column": "id", "data_type": "int", "key": "PRI"},
        {"column": "shop_id", "data_type": "int", "key": ""},
        {"column": "offer_heading", "data_type": "text", "key": ""},
        {"column": "offer_price", "data_type": "decimal", "key": ""},
    ],
}
OFFER_RELATIONSHIPS = RELATIONSHIPS + [{
    "constraint_name": "reviewed_offer_shop_id",
    "table": "offer",
    "referenced_table": "shop_details",
    "cardinality": "many_to_one",
    "verification": "reviewed_manual_query",
    "column_pairs": [{"column": "shop_id", "referenced_column": "id"}],
}]


def _juice_plan(monkeypatch, question, ids, original=""):
    calls = []

    async def fake_fetch(sql, params=()):
        calls.append((sql, params))
        return [(i,) for i in ids]

    async def fake_cities():
        return list(KNOWN_CITIES)

    monkeypatch.setattr(gq, "fetch_all_readonly", fake_fetch)
    monkeypatch.setattr(gq, "_load_known_cities", fake_cities)
    plan = asyncio.run(
        gq._build_cheap_juice_query((question, original), OFFER_SCHEMA, OFFER_RELATIONSHIPS)
    )
    return plan, calls


def test_juice_plan_shape(monkeypatch):
    plan, calls = _juice_plan(monkeypatch, "any cheap juice places in Nagercoil?", [105, 130])
    assert plan["table"] == "offer"
    assert [j["table"] for j in plan["joins"]] == ["shop_details"]
    assert plan["where_expression"]["conditions"][0]["value"] == [105, 130]
    assert plan["order_by"][0] == {"column": "offer_price", "direction": "ASC"}
    assert plan["limit"] == 10
    assert calls[0][1] == ("%juice%", "Nagercoil", "Nahercoil")
    assert "sd.status = 1" in calls[0][0] and "is_default = 1" in calls[0][0]


def test_juice_not_triggered_without_juice_cheap_or_city(monkeypatch):
    for question in (
        "any cheap pizza places in Nagercoil?",
        "any juice places in Nagercoil?",
        "any cheap juice places?",
        "how many cheap juice places in Nagercoil",
    ):
        plan, calls = _juice_plan(monkeypatch, question, [1])
        assert plan is None and calls == []


def test_juice_city_can_come_from_original_question(monkeypatch):
    plan, _ = _juice_plan(
        monkeypatch,
        "Show cheap juice shops",
        [105],
        original="cheap juice places in Nagercoil",
    )
    assert plan is not None


def test_juice_no_shops_returns_zero_row_plan(monkeypatch):
    plan, _ = _juice_plan(monkeypatch, "cheap juice places in Kazhakkoottam", [])
    assert plan["where_expression"]["conditions"][0]["value"] == [0]
    assert plan["grader_note"] is None


def test_juice_note_says_ranked_list_and_text_match(monkeypatch):
    plan, _ = _juice_plan(monkeypatch, "cheap juice places in Nagercoil", [105])
    note = plan["grader_note"]
    assert "ranked list was returned deliberately" in note
    assert "does not verify" in note


def test_juice_plan_passes_validation_and_safety(monkeypatch):
    plan, _ = _juice_plan(monkeypatch, "cheap juice places in Nagercoil", [105, 130])
    plan.pop("grader_note")
    validated = gq._validate_llm_output(
        plan, OFFER_SCHEMA, OFFER_RELATIONSHIPS, "cheap juice places in Nagercoil", []
    )
    result = run_safety_check(validated, list(OFFER_SCHEMA), OFFER_SCHEMA, OFFER_RELATIONSHIPS)
    assert "shop_address" not in result["sql"]
    assert "ORDER BY" in result["sql"]
