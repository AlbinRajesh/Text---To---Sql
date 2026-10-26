import sys
import os
import json
import re

sys.path.insert(0, os.path.abspath('backend'))

# Load patches definitions
# Each patch has:
# id, name, file_line, trigger_type ("static_regex", "static_function", "dynamic_ai", "prompt_text"),
# trigger_fn (callable taking question_text -> bool/match),
# behavior, business_fact

import app.agent.nodes.generate_query as gq
import app.agent.catalog_resolver as cr
import app.agent.nodes.classifier_rewrite as clr

PATCHES = [
    {
        "id": "P01",
        "name": "SQL Injection Preflight Refusal",
        "file_line": "backend/app/agent/nodes/generate_query.py:227",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._SQL_INJECTION_PATTERN.search(q)),
        "match_fn": lambda q: gq._SQL_INJECTION_PATTERN.findall(q),
        "behavior": "Raises UnanswerableQuestionError refusing destructive commands",
        "business_fact": "Security guardrail: destructive SQL statements cannot be processed."
    },
    {
        "id": "P02",
        "name": "Product Reviews Preflight Refusal",
        "file_line": "backend/app/agent/nodes/generate_query.py:233",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._PRODUCT_REVIEWS_PATTERN.search(q)),
        "match_fn": lambda q: gq._PRODUCT_REVIEWS_PATTERN.findall(q),
        "behavior": "Raises UnanswerableQuestionError refusing review/rating questions on products",
        "business_fact": "Products do not have reviews in schema; reviews attach to shops."
    },
    {
        "id": "P03",
        "name": "Shop Active Status / Verified Address Preflight Refusal",
        "file_line": "backend/app/agent/nodes/generate_query.py:242",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._SHOP_ACTIVE_STATUS_PATTERN.search(q) or gq._VERIFIED_ADDRESS_PATTERN.search(q)),
        "match_fn": lambda q: (gq._SHOP_ACTIVE_STATUS_PATTERN.findall(q) + gq._VERIFIED_ADDRESS_PATTERN.findall(q)),
        "behavior": "Raises UnanswerableQuestionError for active shop or verified address",
        "business_fact": "Shop active status and verified address definitions (unsupported in case 22)."
    },
    {
        "id": "P04",
        "name": "Relative Date Preflight Refusal",
        "file_line": "backend/app/agent/nodes/generate_query.py:254",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._RELATIVE_DATE_PATTERN.search(q)),
        "match_fn": lambda q: gq._RELATIVE_DATE_PATTERN.findall(q),
        "behavior": "Raises UnanswerableQuestionError refusing relative date tokens",
        "business_fact": "Relative dates require explicit timezone/calendar anchor."
    },
    {
        "id": "P05",
        "name": "Unsupported Savings Currency Threshold Preflight Refusal",
        "file_line": "backend/app/agent/nodes/generate_query.py:114",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._UNSUPPORTED_SAVINGS_PATTERN.search(q)),
        "match_fn": lambda q: gq._UNSUPPORTED_SAVINGS_PATTERN.findall(q),
        "behavior": "Raises UnanswerableQuestionError refusing savings with currency amounts",
        "business_fact": "Savings threshold currency filters are unsupported."
    },
    {
        "id": "P06",
        "name": "Hardcoded Ranked Rating Query Builder",
        "file_line": "backend/app/agent/nodes/generate_query.py:3879",
        "type": "static_function",
        "trigger": lambda q: gq.is_ranked_rating_request((q,)),
        "match_fn": lambda q: ["ranked_rating_bypass"] if gq.is_ranked_rating_request((q,)) else [],
        "behavior": "Bypasses LLM query generation; builds hardcoded AVG(rating) SQL with city shop_id IN clause",
        "business_fact": "Best rated shops in a city means AVG(reviews.rating) DESC with shop_address.city."
    },
    {
        "id": "P07",
        "name": "Hardcoded Cheap Juice Query Builder",
        "file_line": "backend/app/agent/nodes/generate_query.py:4049",
        "type": "static_function",
        "trigger": lambda q: gq.is_cheap_juice_request((q,)),
        "match_fn": lambda q: ["cheap_juice_bypass"] if gq.is_cheap_juice_request((q,)) else [],
        "behavior": "Bypasses LLM query generation; builds hardcoded shop_details.name LIKE '%juice%' query",
        "business_fact": "Juice shops identified by shop_details.name LIKE '%juice%' in given city."
    },
    {
        "id": "P08",
        "name": "Trade Subcategory Mechanic Hardcoding",
        "file_line": "backend/app/agent/nodes/generate_query.py:3746",
        "type": "static_regex",
        "trigger": lambda q: any(p.search(q) for p, _ in gq._TRADE_SUBCATEGORIES),
        "match_fn": lambda q: [s for p, s in gq._TRADE_SUBCATEGORIES if p.search(q)],
        "behavior": "Forces subcategory 'Bike & Car Servicing' for mechanic queries",
        "business_fact": "Mechanics correspond to subcategory 'Bike & Car Servicing'."
    },
    {
        "id": "P09",
        "name": "Alternate Address Scope Heuristic Regex",
        "file_line": "backend/app/agent/nodes/generate_query.py:2299",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._ALTERNATE_ADDRESS_SCOPE_PATTERN.search(q)),
        "match_fn": lambda q: gq._ALTERNATE_ADDRESS_SCOPE_PATTERN.findall(q),
        "behavior": "Drops mandatory shop_address.is_default = 1 filter for multi-address requests",
        "business_fact": "Queries asking for multiple/all addresses must omit is_default = 1."
    },
    {
        "id": "P10",
        "name": "User Active Subscription Validator Pattern",
        "file_line": "backend/app/agent/nodes/generate_query.py:124",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._USER_ACTIVE_SUBSCRIPTION_PATTERN.search(q)),
        "match_fn": lambda q: gq._USER_ACTIVE_SUBSCRIPTION_PATTERN.findall(q),
        "behavior": "Forces subscriptions.status = 'active' and forbids users.subscriber_status",
        "business_fact": "Active subscription means subscriptions.status = 'active'."
    },
    {
        "id": "P11",
        "name": "Plan Assignment Validator Pattern",
        "file_line": "backend/app/agent/nodes/generate_query.py:148",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._PLAN_ASSIGNMENT_PATTERN.search(q)),
        "match_fn": lambda q: gq._PLAN_ASSIGNMENT_PATTERN.findall(q),
        "behavior": "Forces users.plan_id IS NOT NULL in where_expression",
        "business_fact": "Assigned plan requires users.plan_id IS NOT NULL."
    },
    {
        "id": "P12",
        "name": "Active Offer Intent Status Validator",
        "file_line": "backend/app/agent/nodes/generate_query.py:98",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._ACTIVE_OFFER_INTENT_PATTERN.search(q)),
        "match_fn": lambda q: gq._ACTIVE_OFFER_INTENT_PATTERN.findall(q),
        "behavior": "Requires offer.status = 1 and blocks date conditions on offer",
        "business_fact": "Active offers are defined strictly by offer.status = 1."
    },
    {
        "id": "P13",
        "name": "Active Shop Status Mention Pattern",
        "file_line": "backend/app/agent/nodes/generate_query.py:105",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._ACTIVE_SHOP_MENTION_PATTERN.search(q)),
        "match_fn": lambda q: gq._ACTIVE_SHOP_MENTION_PATTERN.findall(q),
        "behavior": "Forces shop_details.status = 1 filter",
        "business_fact": "Active shops require shop_details.status = 1."
    },
    {
        "id": "P14",
        "name": "Disallow Ratio Intent Refusal",
        "file_line": "backend/app/agent/nodes/generate_query.py:84",
        "type": "static_regex",
        "trigger": lambda q: bool(gq._UNSUPPORTED_RATIO_PATTERN.search(q)),
        "match_fn": lambda q: gq._UNSUPPORTED_RATIO_PATTERN.findall(q),
        "behavior": "Raises UnanswerableQuestionError when the word 'ratio' appears without derived metrics",
        "business_fact": "Direct arbitrary ratio calculations without registered derived metrics are unsupported."
    },
    {
        "id": "P15",
        "name": "Classifier Hardcoded Typo Restaurent & Vocabulary",
        "file_line": "backend/app/agent/prompts/classifier_prompt.py:100",
        "type": "static_regex",
        "trigger": lambda q: bool(re.search(r"\b(?:restaurants?|food places?|places to eat|dining|car mechanics?)\b", q, re.I)),
        "match_fn": lambda q: re.findall(r"\b(?:restaurants?|food places?|places to eat|dining|car mechanics?)\b", q, re.I),
        "behavior": "Classifier rewrites restaurant to 'Restaurent' and mechanics to 'Bike & Car Servicing'",
        "business_fact": "Restaurants map to subcategory 'Restaurent'; mechanics to 'Bike & Car Servicing'."
    },
    {
        "id": "P16",
        "name": "Classifier Disambiguation Suppression for Self-Contained Queries",
        "file_line": "backend/app/agent/nodes/classifier_rewrite.py:139",
        "type": "static_regex",
        "trigger": lambda q: bool(clr._SELF_CONTAINED_DATA_REQUEST_PATTERN.search(q)),
        "match_fn": lambda q: clr._SELF_CONTAINED_DATA_REQUEST_PATTERN.findall(q),
        "behavior": "Suppresses clarification for self-contained aggregate/ranking requests",
        "business_fact": "Self-contained ranking/aggregates across categories do not need city/entity clarification."
    },
    {
        "id": "P17",
        "name": "Booking Revenue Category Shop Disambiguation Pattern",
        "file_line": "backend/app/agent/nodes/classifier_rewrite.py:134",
        "type": "static_regex",
        "trigger": lambda q: bool(clr._BOOKING_REVENUE_CATEGORY_PATTERN.search(q)),
        "match_fn": lambda q: clr._BOOKING_REVENUE_CATEGORY_PATTERN.findall(q),
        "behavior": "Forces 'category' in booking revenue questions to mean shop category",
        "business_fact": "Booking revenue per category traverses through shop_details.category_id."
    },
    {
        "id": "P18",
        "name": "Catalog Resolver Category Sell/Provide Clause Pattern",
        "file_line": "backend/app/agent/catalog_resolver.py:76",
        "type": "static_regex",
        "trigger": lambda q: bool(cr._SELL_CLAUSE.search(q) or cr._PROVIDED_SERVICE_CLAUSE.search(q)),
        "match_fn": lambda q: [m.group(0) for m in cr._SELL_CLAUSE.finditer(q)] + [m.group(0) for m in cr._PROVIDED_SERVICE_CLAUSE.finditer(q)],
        "behavior": "Extracts candidate search terms following sell/provide verbs",
        "business_fact": "Phrases like 'sell X' or 'provide Y' indicate product/service catalog search scope."
    },
    {
        "id": "P19",
        "name": "Prompt Prompt Case #39 Unscoped Percentage Instruction",
        "file_line": "backend/app/agent/prompts/generate_query_prompt.py:193",
        "type": "static_regex",
        "trigger": lambda q: bool(re.search(r"\bpercentage of reviews rated\b", q, re.I)),
        "match_fn": lambda q: re.findall(r"\bpercentage of reviews rated\b", q, re.I),
        "behavior": "Forces where_expression: null for unscoped percentage query",
        "business_fact": "Unscoped percentage has denominator equal to all base table rows."
    },
    {
        "id": "P20",
        "name": "Prompt Monthly Booking Timestamp Disambiguation",
        "file_line": "backend/app/agent/prompts/generate_query_prompt.py:139",
        "type": "static_regex",
        "trigger": lambda q: bool(re.search(r"\b(?:monthly\s+booking|bookings?.*(?:each|every|per)\s+month)\b", q, re.I)),
        "match_fn": lambda q: re.findall(r"\b(?:monthly\s+booking|bookings?.*(?:each|every|per)\s+month)\b", q, re.I),
        "behavior": "Directs query generator to use time_slot_month vs created_at",
        "business_fact": "Booking date vs transaction creation date temporal mapping."
    }
]

# Run static match
with open('backend/app/tests/patch_audit/merged_questions.json', 'r', encoding='utf-8') as f:
    questions = json.load(f)

matrix_rows = []
match_counts = {p["id"]: 0 for p in PATCHES}
question_matches = {q["id"]: [] for q in questions}

for q in questions:
    q_text = q.get("question", "")
    row = {
        "question_id": q["id"],
        "source": q["source"],
        "question": q_text,
        "matched_patches": []
    }
    for p in PATCHES:
        matched = False
        try:
            matched = p["trigger"](q_text)
        except Exception as e:
            matched = False
        if matched:
            match_details = p["match_fn"](q_text)
            row["matched_patches"].append(p["id"])
            match_counts[p["id"]] += 1
            question_matches[q["id"]].append((p["id"], str(match_details)))
    matrix_rows.append(row)

# Save matrix CSV
import csv
with open('backend/app/tests/patch_audit/question_patch_matrix.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    header = ["question_id", "source", "question", "matched_patch_count", "matched_patches"] + [p["id"] for p in PATCHES]
    writer.writerow(header)
    for r in matrix_rows:
        writer.writerow([
            r["question_id"],
            r["source"],
            r["question"],
            len(r["matched_patches"]),
            " ".join(r["matched_patches"])
        ] + [("1" if p["id"] in r["matched_patches"] else "0") for p in PATCHES])

# Save raw matrix json
with open('backend/app/tests/patch_audit/question_patch_matrix.json', 'w', encoding='utf-8') as f:
    json.dump({
        "summary": match_counts,
        "questions": matrix_rows
    }, f, indent=2)

print("Static match complete!")
print("Patch match counts:")
for p in PATCHES:
    print(f"  {p['id']} ({p['name']}): {match_counts[p['id']]} questions matched")
