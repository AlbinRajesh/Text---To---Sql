# Patch Ledger: Audit of Question-Specific Patches & Workarounds

This ledger catalogs every hardcoded patch, regex gate, procedural query builder, and prompt heuristic identified across the repository.

---

## 1. Patch Catalog & Inventory

### [P01] SQL Injection Preflight Refusal
- **Location:** `backend/app/agent/nodes/generate_query.py:227`
- **Trigger:** `_SQL_INJECTION_PATTERN` matches `DROP`, `ALTER`, `TRUNCATE`, `DELETE`, `INSERT`, `UPDATE`, `--`, or `/* */`.
- **Behavior:** Raises `UnanswerableQuestionError` refusing destructive SQL.
- **Business Fact Contained:** System is read-only; destructive commands are forbidden.
- **Affected Questions:** `golden_benchmark_catalog_G_24`, `golden_benchmark_catalog_2_G_22`.
- **Effect of Removal:** Dangerous (allows DDL/DML injection attacks to reach later stages). Keep as universal security boundary.

### [P02] Product Reviews Preflight Refusal
- **Location:** `backend/app/agent/nodes/generate_query.py:233`
- **Trigger:** `_PRODUCT_REVIEWS_PATTERN` matches `product(s) ... review(s)/rating(s)`.
- **Behavior:** Raises `UnanswerableQuestionError` ("Products do not have direct review ratings in the available database schema; reviews are attached to shops.").
- **Business Fact Contained:** Reviews in this database belong to `shop_details`, not `products`.
- **Affected Questions:** `golden_benchmark_catalog_G_22`.
- **Effect of Removal:** Same (or unanswerable by schema reachability check). Can be cleanly replaced by graph reachability in schema grounding.

### [P03] Shop Active Status & Verified Address Preflight Refusal
- **Location:** `backend/app/agent/nodes/generate_query.py:242`
- **Trigger:** `_SHOP_ACTIVE_STATUS_PATTERN` or `_VERIFIED_ADDRESS_PATTERN` matches "currently active shops" or "verified address".
- **Behavior:** Raises `UnanswerableQuestionError` claiming active status/verified address is not defined.
- **Business Fact Contained:** Legacy assumption from test case 22 before active status rules were formalized.
- **Affected Questions:** 0 active questions in current benchmark suite.
- **Effect of Removal:** Safe / Better (active shop status is now fully supported via `shop_details.status = 1` in `business_rules.json`).

### [P04] Relative Date Preflight Refusal
- **Location:** `backend/app/agent/nodes/generate_query.py:254`
- **Trigger:** `_RELATIVE_DATE_PATTERN` matches "today", "yesterday", "tomorrow", "this month", "past 7 days", etc.
- **Behavior:** Raises `UnanswerableQuestionError` demanding explicit calendar dates.
- **Business Fact Contained:** Relative dates require timezone and database calendar anchoring.
- **Affected Questions:** 14 questions (e.g. `T002`, `T007`, `T011`, `T012`, `T013`, `T049`, `T051`, `T056`).
- **Effect of Removal:** Worse if temporal resolver is bypassed; Better if temporal anchor resolver converts relative dates to dynamic subqueries or scalar date bounds.

### [P05] Unsupported Savings Currency Threshold Preflight Refusal
- **Location:** `backend/app/agent/nodes/generate_query.py:114`
- **Trigger:** `_UNSUPPORTED_SAVINGS_PATTERN` matches "save > 500 rupees", etc.
- **Behavior:** Raises `UnanswerableQuestionError` refusing numeric currency thresholds on savings.
- **Business Fact Contained:** Fixed monetary savings thresholds across offers without price baselines are ambiguous.
- **Affected Questions:** 0 in current benchmark suite.
- **Effect of Removal:** Safe to delete (handled naturally by metric type validation).

### [P06] Hardcoded Ranked Rating Query Builder
- **Location:** `backend/app/agent/nodes/generate_query.py:3879-4006`
- **Trigger:** `is_ranked_rating_request` matches superlative rating questions in a city with words from `_RANKED_ALLOWED_WORDS`.
- **Behavior:** Bypasses LLM entirely; runs two procedural MySQL queries (`_lookup_city_shop_ids`) and outputs a hardcoded query plan targeting `shop_id IN (...)`.
- **Business Fact Contained:** Top rated shops in a city = `AVG(reviews.rating) DESC` filtered by `shop_address.city` and `sa.is_default = 1`.
- **Affected Questions:** `cases_batch4_E2E_0`, `cases_batch4_E2E_1`.
- **Effect of Removal:** Different-but-correct (LLM must synthesize standard SQL join: `reviews JOIN shop_details JOIN shop_address`).

### [P07] Hardcoded Cheap Juice Query Builder
- **Location:** `backend/app/agent/nodes/generate_query.py:4049-4140`
- **Trigger:** `is_cheap_juice_request` matches "cheap/cheapest" and "juice" with a city.
- **Behavior:** Bypasses LLM entirely; runs procedural lookup for `sd.name LIKE '%juice%'` and returns pre-canned query plan.
- **Business Fact Contained:** Juice places are shops whose name contains "juice"; cheapest offers = `ORDER BY offer_price ASC`.
- **Affected Questions:** `test_2/case_main.json` juice search questions.
- **Effect of Removal:** Different-but-correct (LLM generates query using text search or catalog rules).

### [P08] Trade Subcategory Mechanic Hardcoding
- **Location:** `backend/app/agent/nodes/generate_query.py:3746-3749`
- **Trigger:** `_TRADE_SUBCATEGORIES` regex matching "car/bike mechanic".
- **Behavior:** Directly hardcodes mapping to subcategory `"Bike & Car Servicing"`.
- **Business Fact Contained:** Mechanics are filed under subcategory `"Bike & Car Servicing"`.
- **Affected Questions:** Mechanic search questions.
- **Effect of Removal:** Worse if alias is missing from config; Same if moved to `catalog_aliases.json`.

### [P09] Alternate Address Scope Heuristic Regex
- **Location:** `backend/app/agent/nodes/generate_query.py:2299-2315`
- **Trigger:** `_ALTERNATE_ADDRESS_SCOPE_PATTERN` matches "all/any/multiple/different addresses".
- **Behavior:** Allows query to omit `shop_address.is_default = 1`.
- **Business Fact Contained:** Multi-address queries require inspecting all addresses, not just default address.
- **Affected Questions:** `T078` ("do any shop addresses have no map location saved?").
- **Effect of Removal:** Worse (without general rule, T078 validator fails with `address_requires_default`).

### [P10] User Active Subscription Validator Pattern
- **Location:** `backend/app/agent/nodes/generate_query.py:124`
- **Trigger:** `_USER_ACTIVE_SUBSCRIPTION_PATTERN` matches "users with active subscription".
- **Behavior:** Rejects query if it doesn't join `subscriptions` with `status = 'active'`.
- **Business Fact Contained:** Active subscription = `subscriptions.status = 'active'`.
- **Affected Questions:** Subscription queries.
- **Effect of Removal:** Same (handled by business rules file).

### [P11] Plan Assignment Validator Pattern
- **Location:** `backend/app/agent/nodes/generate_query.py:148`
- **Trigger:** `_PLAN_ASSIGNMENT_PATTERN` matches "assigned plan" / "plan assigned".
- **Behavior:** Enforces `users.plan_id IS NOT NULL`.
- **Business Fact Contained:** Users with an assigned plan must have `plan_id IS NOT NULL`.
- **Affected Questions:** `T040`.
- **Effect of Removal:** Same if declared in `business_rules.json`.

### [P12] Active Offer Intent Status Validator
- **Location:** `backend/app/agent/nodes/generate_query.py:98`
- **Trigger:** `_ACTIVE_OFFER_INTENT_PATTERN` matches "active offer", "currently active", etc.
- **Behavior:** Mandates `offer.status = 1` and blocks date conditions on `offer`.
- **Business Fact Contained:** Active offer status is defined by `offer.status = 1`.
- **Affected Questions:** 15 questions (`T020`, `T027`, `T052`, `T053`, `T056`, etc.).
- **Effect of Removal:** Worse for date conflict questions; Better once date-conflict exception is relaxed for questions with explicit temporal bounds (e.g. `T056`).

### [P13] Active Shop Status Mention Pattern
- **Location:** `backend/app/agent/nodes/generate_query.py:105`
- **Trigger:** `_ACTIVE_SHOP_MENTION_PATTERN` matches "active shop".
- **Behavior:** Enforces `shop_details.status = 1`.
- **Business Fact Contained:** Active shop status is defined by `shop_details.status = 1`.
- **Affected Questions:** 51 questions (`T015`, `T025`, `T027`, `T029`, `T061`, etc.).
- **Effect of Removal:** Worse (if active shop filter is omitted); Same if driven cleanly from `business_rules.json`.

### [P14] Disallow Ratio Intent Refusal
- **Location:** `backend/app/agent/nodes/generate_query.py:84`
- **Trigger:** `_UNSUPPORTED_RATIO_PATTERN` matches "ratio".
- **Behavior:** Refuses questions containing the word "ratio" unless using typed derived metrics.
- **Business Fact Contained:** Arbitrary ratios without formal numerator/denominator contracts cannot be guaranteed mathematically.
- **Affected Questions:** 0 in benchmark.
- **Effect of Removal:** Safe.

### [P15] Classifier Hardcoded Typo Restaurent & Vocabulary
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:100-123`
- **Trigger:** Questions containing "restaurant", "food places", "dining", "car mechanic".
- **Behavior:** Prompt explicitly instructs the LLM to rewrite "restaurant" to `"Restaurent"` (misspelling in DB) and "car mechanic" to `"Bike & Car Servicing"`.
- **Business Fact Contained:** Data spelling quirk: subcategory is spelled `"Restaurent"`.
- **Affected Questions:** 13 questions (`T003`, `T006`, `T012`, `T072`, `cases_E2E_7`, etc.).
- **Effect of Removal:** Worse if removed without taxonomy lookup; Same if `catalog_aliases.json` handles the mapping.

### [P16] Classifier Disambiguation Suppression for Self-Contained Queries
- **Location:** `backend/app/agent/nodes/classifier_rewrite.py:139`
- **Trigger:** `_SELF_CONTAINED_DATA_REQUEST_PATTERN` matches percentage or ranked requests across categories.
- **Behavior:** Prevents the classifier from asking clarifying questions about cities or entities.
- **Business Fact Contained:** Analytical aggregations across categories imply all cities by default.
- **Affected Questions:** 1 question (`golden_benchmark_catalog_G_11`).
- **Effect of Removal:** Worse (classifier will stall with unnecessary clarification).

### [P17] Booking Revenue Category Shop Disambiguation Pattern
- **Location:** `backend/app/agent/nodes/classifier_rewrite.py:134`
- **Trigger:** `_BOOKING_REVENUE_CATEGORY_PATTERN` matches "category" and "booking revenue".
- **Behavior:** Forces "category" to mean shop category via `shop_details.category_id`.
- **Business Fact Contained:** Booking revenue per category resolves via `bookings -> shop_details -> categories`.
- **Affected Questions:** 5 questions (`golden_benchmark_catalog_2_G_11`, `golden_benchmark_catalog_3_G_1`, etc.).
- **Effect of Removal:** Worse (classifier confuses offer category vs shop category).

### [P18] Catalog Resolver Category Sell/Provide Clause Pattern
- **Location:** `backend/app/agent/catalog_resolver.py:76`
- **Trigger:** `_SELL_CLAUSE` / `_PROVIDED_SERVICE_CLAUSE` matching "sell X" / "provide Y".
- **Behavior:** Extracts candidate terms following sell/provide verbs.
- **Business Fact Contained:** "sell X" indicates product/catalog scope.
- **Affected Questions:** 6 questions (`T022`, `T054`, `T121`, `cases_E2E_13`, etc.).
- **Effect of Removal:** Same / Better if catalog entity extraction uses full-phrase semantic matching.

### [P19] Prompt Case #39 Unscoped Percentage Instruction
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:193`
- **Trigger:** Mentions "case #39" for "percentage of reviews rated five stars".
- **Behavior:** Instructs model to set `where_expression: null` when no denominator scope is present.
- **Business Fact Contained:** Unscoped `share_of_rows` has denominator = all base table rows.
- **Affected Questions:** 0 direct regex match (guidance applies to unscoped percentage queries).
- **Effect of Removal:** Safe / Better (replace "case #39" reference with standard contract description).

### [P20] Prompt Monthly Booking Timestamp Disambiguation
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:139`
- **Trigger:** Questions asking for "monthly bookings" or bookings per month.
- **Behavior:** Directs generator to use `time_slot_month` for appointment month vs `created_at` for record creation date.
- **Business Fact Contained:** Temporal event grain: appointment date (`time_slot_date`) vs realization timestamp (`created_at`).
- **Affected Questions:** 4 questions (`T079`, `golden_benchmark_catalog_2_G_5`, etc.).
- **Effect of Removal:** Worse if omitted; Same if defined in `business_rules.json`.
