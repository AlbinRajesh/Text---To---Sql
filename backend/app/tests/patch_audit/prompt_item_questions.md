# Prompt Item Question Impact Matrix (Read-Only Audit)

This document provides a comprehensive audit of all hardcoded prompt text items across `classifier_prompt.py`, `generate_query_prompt.py`, and `grader_prompt.py`, mapping each item directly to the test questions in `backend/app/tests/test_2/case_main.json` that it affects.

---

## 1. Hardcoded Items and Affected Questions

### [H01] Subcategory Spelling Quirk: "Restaurent"
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:101-102, 118, 121, 123`
- **Quote [VERIFIED]:**
  > `"restaurant", "restaurants", "food places", "places to eat", "dining" mean shops in the subcategory named "Restaurent" (it is spelled that way in the data).`
  > `"show me restaurants" -> "Show shops whose subcategory is 'Restaurent'"`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T003** | Are there any good restaurants in Colachel? | DIRECT | Golden SQL: `JOIN subcategories sc ... sc.subcategoryname = 'Restaurent'` |
| **T012** | What's the cheapest offer available at any restaurant in Nagercoil right now? | DIRECT | Golden SQL: `JOIN subcategories sc ... sc.subcategoryname = 'Restaurent'` |
| **T072** | are there any hotels that aren't listed as restaurants? | DIRECT | Context / Golden SQL: checks against `sc.subcategoryname = 'Restaurent'` |
| **T006** | Show me all the vegan restaurants. | LIKELY | Question asks for `"vegan restaurants"`, which routes to subcategory `"Restaurent"` or name search |

- **Count:** 4 questions (3 DIRECT, 1 LIKELY)

---

### [H02] Subcategory Mapping: "Bike & Car Servicing" (Mechanics)
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:111-112`
- **Quote [VERIFIED]:**
  > `"car mechanic", "car mechanics" mean shops in the subcategory named "Bike & Car Servicing".`
- **Affected Questions:**
*(None in `case_main.json`. Present in `regression_run.py` and `test_ranked_rating_query.py`)*

- **Count:** 0 questions in `case_main.json`

---

### [H03] Category Mapping: "Services"
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:107-108, 120`
- **Quote [VERIFIED]:**
  > `A direct request to show services means shops in the category named "Services", unless the user names a specific service type.`
  > `"show me services in Nagercoil" -> "Show shops in the category named 'Services' in Nagercoil"`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T097** | Show me all bookings made by male users for beauty services. | LIKELY | Asks for `"beauty services"`, disambiguating between category `"Services"` vs `service` table |

- **Count:** 1 question (0 DIRECT, 1 LIKELY)

---

### [H04] Category Mapping: "Food & Dining"
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:109-110`
- **Quote [VERIFIED]:**
  > `"food" or "food & dining" as a whole category means shops in the category named "Food & Dining".`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T112** | which type of business is most common on our app? | DIRECT | Golden SQL / Origin context evaluates top business categories, including `'Food & Dining'` |

- **Count:** 1 question (1 DIRECT, 0 LIKELY)

---

### [H05] Business-Value Catalog Examples: Drinks, Nagercoil, Kochi
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:103-106, 119, 121`
- **Quote [VERIFIED]:**
  > `Resolve requests for drinks, beverages, restaurants, and services against the supplied business-value catalog.`
  > `"show me drinks in Nagercoil" -> "Show shops whose subcategory is the matching value from the business-value catalog in Nagercoil"`
  > `"restaurants from Kochi" -> "Show shops whose subcategory is 'Restaurent' from Kochi"`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T091** | Show me the Bevereges shops. | DIRECT | Golden SQL / Business Rule: resolves misspelled `"Bevereges"` to catalog value |
| **T012** | What's the cheapest offer available at any restaurant in Nagercoil right now? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T025** | Show me all active shops in Nagercoil. | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T066** | which city is bringing in the most money from bookings? | DIRECT | Golden SQL groups by `sa.city` (Nagercoil is top revenue city) |
| **T082** | how many bookings were made at Nagercoil shops? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T087** | Is anyone booking things at cities that are not Nagercoil or Trivandrum? | DIRECT | Golden SQL: `sa.city NOT IN ('Nagercoil', 'Trivandrum')` |
| **T126** | I'm thirsty, where can I get a juice in Nagercoil? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T127** | craving biriyani, any place in Nagercoil? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T128** | need to buy a new phone, which mobile shops are in Nagercoil? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T129** | I need a ride, any taxi or auto around Nagercoil? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T132** | where can I buy clothes in Nagercoil? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |
| **T135** | any toy shops in Nagercoil for my kid's birthday? | DIRECT | Golden SQL: `sa.city = 'Nagercoil'` |

- **Count:** 12 questions (12 DIRECT, 0 LIKELY)

---

### [H06] Verbatim Clarification Suppression Examples
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:58-62`
- **Quote [VERIFIED]:**
  > `For example, "What percentage of offers have a discount greater than 50 percent?" means all offers, and "Which subcategories have the most active shops in Nagercoil?" asks for subcategories ranked by active-shop count.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T025** | Show me all active shops in Nagercoil. | LIKELY | Verbatim pattern suppression prevents classifier from asking for clarification in Nagercoil active shop queries |
| **T070** | on average, what percent discount do we give per booking? | LIKELY | Prevents percentage discount requests from stalling on clarification |

- **Count:** 2 questions (0 DIRECT, 2 LIKELY)

---

### [H07] Booking Revenue Category Traversal Instruction
- **Location:** `backend/app/agent/prompts/classifier_prompt.py:65-67`
- **Quote [VERIFIED]:**
  > `When the user asks which category generated booking revenue, interpret "category" as the shop category associated with each booking through its shop, unless the user explicitly specifies another category type.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T014** | Which category has the highest average booking amount? | DIRECT | Golden SQL: `JOIN shop_details sd ON sd.id = b.shop_id JOIN categories c ON c.id = sd.category_id` |
| **T021** | Calculate the total revenue generated by each subcategory in the last 6 months. | DIRECT | Golden SQL: `JOIN shop_details sd ON sd.id = b.shop_id JOIN subcategories sc ON sc.id = sd.subcategory_id` |
| **T090** | List the top 3 most commonly booked categories. | DIRECT | Golden SQL: `JOIN shop_details sd ON sd.id = b.shop_id JOIN categories c ON c.id = sd.category_id` |

- **Count:** 3 questions (3 DIRECT, 0 LIKELY)

---

### [H08] Subscription and Plan Assignment Rules
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:93-96`
- **Quote [VERIFIED]:**
  > `For questions about users who have an active subscription, use the verified users-to-subscriptions relationship and filter subscriptions.status = 'active'. Do not substitute users.subscriber_status for a subscription row. If the question also requires a plan to be assigned, require users.plan_id IS NOT NULL.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T013** | How many users have a subscription plan right now? | DIRECT | Golden SQL: `subscriptions` joined or `users.plan_id IS NOT NULL` |
| **T028** | What's the most expensive subscription plan we offer? | DIRECT | Golden SQL: `FROM plans` |
| **T030** | How much potential recurring revenue do we have from active plans? | DIRECT | Golden SQL: `plans` where `status = 1` |
| **T035** | How many users are subscribed to each plan? | DIRECT | Golden SQL: `JOIN plans p ON p.id = u.plan_id` |
| **T040** | How many active users do not have a plan assigned? | DIRECT | Golden SQL: `status = 1 AND (plan_id IS NULL OR plan_id = 0)` |
| **T041** | How many users are on each active plan? | DIRECT | Golden SQL: `users.plan_id = plans.id` |
| **T005** | Who bought a premium plan and also left a 5-star review for a cafe in Trivandrum? | DIRECT | Golden SQL: `users.plan_id = plans.id` |
| **T018** | Which plan is the most popular among our users? | DIRECT | Golden SQL: `users.plan_id` count grouped by `plan_id` |
| **T029** | Show me all the partners who are on a paid plan but have 0 active shops. | DIRECT | Golden SQL: `users.plan_id IS NOT NULL` |
| **T037** | Which plans allow for more than 5 products? | DIRECT | Golden SQL: `FROM plans WHERE product_limit > 5` |
| **T044** | List the names of all the plans we currently have. | DIRECT | Golden SQL: `FROM plans` |
| **T089** | What is the average price of plans currently active? | DIRECT | Golden SQL: `FROM plans WHERE status = 1` |

- **Count:** 12 questions (12 DIRECT, 0 LIKELY)

---

### [H09] Rating Ranking Policy: "No Minimum Reviews"
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:20-23, 119, 362-364`
- **Quote [VERIFIED]:**
  > `RATING_RANKING_POLICY = ("Rank shops by AVG(reviews.rating), ignoring NULL ratings; return the review count; do not require a minimum number of reviews.")`
  > `For a highest-rated shop request, use this rating definition: {RATING_RANKING_POLICY}`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T003** | Are there any good restaurants in Colachel? | DIRECT | Golden SQL: `GROUP BY sd.id, sd.name HAVING AVG(r.rating) >= 4 ORDER BY avg_rating DESC` |
| **T005** | Who bought a premium plan and also left a 5-star review for a cafe in Trivandrum? | DIRECT | Golden SQL: filters on `r.rating = 5` |
| **T010** | Show me the top 3 cities with the highest number of 5-star reviews. | DIRECT | Golden SQL: `WHERE r.rating = 5` |
| **T017** | I want to see the names of people who booked an offer at 'Timepass' and left a review less than 3 stars. | DIRECT | Golden SQL: `WHERE r.rating < 3` |
| **T019** | Show me the top 5 highest rated shops that have absolutely zero bookings. | DIRECT | Golden SQL: `ROUND(AVG(r.rating), 2) AS avg_rating ... ORDER BY avg_rating DESC` |
| **T067** | which shops in Trivandrum have the worst ratings? | DIRECT | Golden SQL: `AVG(r.rating) ... ORDER BY avg_rating ASC` |
| **T068** | which shop got a 1-star review, and what did the person write? | DIRECT | Golden SQL: `WHERE r.rating = 1` |
| **T075** | which shops have a perfect 5-star rating from more than 10 reviews? | DIRECT | Golden SQL: `HAVING AVG(r.rating) = 5 AND COUNT(r.id) > 10` |
| **T085** | what are people saying about A1 Juice house? | DIRECT | Golden SQL: retrieves reviews for specific shop |
| **T109** | Which shops have a review rating, but have zero bookings? | DIRECT | Golden SQL: `AVG(r.rating)` with zero bookings anti-join |
| **T114** | which shop do customers write the most reviews for? | DIRECT | Golden SQL: `COUNT(r.id) AS review_count ORDER BY review_count DESC` |
| **T116** | someone gave a really bad rating, which shop was it for? | DIRECT | Golden SQL: `WHERE r.rating = 1` |
| **T124** | did anyone leave a 2 star review? | DIRECT | Golden SQL: `WHERE r.rating = 2` |

- **Count:** 13 questions (13 DIRECT, 0 LIKELY)

---

### [H10] Active Offer Definition: offer.status = 1 & Shop Status
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:221-228`
- **Quote [VERIFIED]:**
  > `For offers with an active status, follow the business rule exactly: offer.status = 1. Do not infer a date condition or filter shop_details.status. Do not filter offer.status for general wording such as "posted offers" or "offers"; include all statuses unless the question explicitly asks for active or current offers. Shop discovery still uses the active-shop policy. To count reviews for shops with qualifying offers, use reviews as the base table, join through verified relationships, filter offer.status = 1, and count DISTINCT reviews.id to avoid multiplying review rows by offers.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T008** | List the top 5 most viewed offers. | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T012** | What's the cheapest offer available at any restaurant in Nagercoil right now? | DIRECT | Golden SQL: `WHERE o.status = 1 AND sd.status = 1` |
| **T020** | What are the 3 most recently posted active offers? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T022** | How many shops offer a 'free' service? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T027** | Which active shops have at least one active offer but no reviews yet? | DIRECT | Golden SQL / Origin context: `sd.status = 1 AND o.status = 1` |
| **T046** | Give me an overview of all our editable offers. | DIRECT | Golden SQL: `WHERE offer_status = 1` |
| **T047** | Which shop has the highest number of editable offers? | DIRECT | Golden SQL: `WHERE offer_status = 1` |
| **T050** | Find the 5 most expensive products we have listed. | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T051** | What is the most popular service offered right now? | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T052** | How many active offers have a redemption limit set? | DIRECT | Golden SQL: `WHERE status = 1 AND redemption_limit > 0` |
| **T053** | How many active offers use each coupon type? | DIRECT | Golden SQL: `WHERE status = 1 GROUP BY coupon_type` |
| **T054** | How many shops have both an active product offer and an active service offer? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T056** | Which active offers end this month? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T057** | What is the average discount provided by each shop category? | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T059** | Are there any offers priced at zero? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T062** | How many quick mode offers do we have active right now? | DIRECT | Golden SQL: `WHERE status = 1 AND is_quick_mode = 1` |
| **T098** | List all the offers that start tomorrow. | DIRECT | Golden SQL: `WHERE status = 1` |
| **T099** | Find shops where their actual price and coupon price are exactly the same. | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T101** | Are there any offers with missing images? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T113** | which shops haven't put up any offers yet? | LIKELY | Relies on existence of offers without status filter |
| **T118** | which offers has nobody looked at? | LIKELY | Queries offers table with left join to views |
| **T119** | what's the priciest deal on the app? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T122** | are there any offers on cars or vehicles right now? | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T123** | is there any deal that costs more than 60000 rupees? | DIRECT | Golden SQL: `WHERE status = 1` |
| **T125** | any offers on gold or jewellery? | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T127** | craving biriyani, any place in Nagercoil? | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T130** | looking for a cupboard, any deals going? | DIRECT | Golden SQL: `WHERE o.status = 1 AND sd.status = 1` |
| **T131** | which deal saves me the most money? | DIRECT | Golden SQL: `WHERE o.status = 1` |
| **T133** | I want to learn coding, are there any courses on offer? | DIRECT | Golden SQL: `WHERE o.status = 1 AND sd.status = 1` |
| **T135** | any toy shops in Nagercoil for my kid's birthday? | DIRECT | Golden SQL: `WHERE o.status = 1` |

- **Count:** 30 questions (28 DIRECT, 2 LIKELY)

---

### [H11] Booking Month vs. Created Date Disambiguation
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:139-143`
- **Quote [VERIFIED]:**
  > `For monthly booking summaries, use the scheduled appointment month (bookings.time_slot_month, scoped by bookings.time_slot_date) unless the question explicitly asks when booking records were created; in that case use bookings.created_at. Do not mix the appointment date filter with the creation timestamp or vice versa.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T002** | How many bookings did we get last month? | DIRECT | Golden SQL: `YEAR(created_at) = ... AND MONTH(created_at) = ...` |
| **T007** | Which shop made the most revenue from completed bookings this year? | DIRECT | Golden SQL: `YEAR(b.created_at) = YEAR(CURRENT_DATE)` |
| **T021** | Calculate the total revenue generated by each subcategory in the last 6 months. | DIRECT | Golden SQL: `b.created_at >= DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH)` |
| **T031** | On the most recent day a booking was made, which users viewed an offer but did not book anything that day? | DIRECT | Golden SQL: subquery evaluates `MAX(DATE(created_at)) FROM bookings` |
| **T043** | How many bookings were made in 2025? | DIRECT | Golden SQL: `created_at >= '2025-01-01' AND created_at < '2026-01-01'` |
| **T079** | how much money did bookings bring in each month? | DIRECT | Golden SQL: `DATE_FORMAT(created_at, '%Y-%m') AS booking_month` |
| **T086** | How many bookings did we have at exactly 12:00 PM today? | DIRECT | Golden SQL: `DATE(created_at) = CURRENT_DATE AND HOUR(created_at) = 12` |
| **T108** | Is anyone booking things at 3 AM? | DIRECT | Golden SQL: `HOUR(created_at) = 3` |

- **Count:** 8 questions (8 DIRECT, 0 LIKELY)

---

### [H12] Default Address Scoping: shop_address.is_default = 1
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:67-72, 117-118, 140-141`
- **Quote [VERIFIED]:**
  > `A shop can have several addresses; only one may be marked as its default. For a distinct count of shops by location, any matching address qualifies and the shop primary key must be counted distinctly. For other queries that join shop_address, filter is_default = 1 to prevent duplicate shop/offer rows, unless the user explicitly asks for all addresses. When showing a shop's address details, use its default address as the representative.`
  > `Exception: shop_address may also be joined below shop_details when the plan filters shop_address.is_default = 1, because that gives one address per shop.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T003** | Are there any good restaurants in Colachel? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T005** | Who bought a premium plan and also left a 5-star review for a cafe in Trivandrum? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T010** | Show me the top 3 cities with the highest number of 5-star reviews. | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T012** | What's the cheapest offer available at any restaurant in Nagercoil right now? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T015** | Find all active shops that have a default address but no phone number listed. | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T016** | What is the average discount given on bookings for each city? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T025** | Show me all active shops in Nagercoil. | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T066** | which city is bringing in the most money from bookings? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T067** | which shops in Trivandrum have the worst ratings? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T071** | which pincode has the most shops? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T078** | do any shop addresses have no map location saved? | DIRECT | Golden SQL: queries `shop_address` (omits `is_default` to check all addresses) |
| **T081** | how many shops have more than one address? | DIRECT | Golden SQL: counts `shop_address` records per shop (omits `is_default`) |
| **T082** | how many bookings were made at Nagercoil shops? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T087** | Is anyone booking things at cities that are not Nagercoil or Trivandrum? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T096** | Which cities have exactly 1 shop registered? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T110** | Just give me everything you have on 'Boss App Studio Pvt Ltd'. | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T111** | how many shops are there in Kanyakumari? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T126** | I'm thirsty, where can I get a juice in Nagercoil? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T127** | craving biriyani, any place in Nagercoil? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T128** | need to buy a new phone, which mobile shops are in Nagercoil? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T129** | I need a ride, any taxi or auto around Nagercoil? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T132** | where can I buy clothes in Nagercoil? | DIRECT | Golden SQL: `sa.is_default = 1` |
| **T135** | any toy shops in Nagercoil for my kid's birthday? | DIRECT | Golden SQL: `sa.is_default = 1` |

- **Count:** 23 questions (23 DIRECT, 0 LIKELY)

---

### [H13] Hand-Typed Metric Whitelist
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:166-167, 207-209`
- **Quote [VERIFIED]:**
  > `Calculated numeric metrics are available only by their exact registered name: final_price, savings, discount_percent, and coupon_discount_percent.`
  > `Use discount_percent for offer-level percentage-off calculations, coupon_discount_percent only for coupon-level percentage-off calculations, and savings for currency amounts.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T012** | What's the cheapest offer available at any restaurant in Nagercoil right now? | DIRECT | Golden SQL evaluates `offer_price` (`final_price` metric) |
| **T050** | Find the 5 most expensive products we have listed. | DIRECT | Golden SQL evaluates `offer_price` |
| **T057** | What is the average discount provided by each shop category? | DIRECT | Golden SQL: evaluates discount on offers (`discount_percent` / `discount_value`) |
| **T059** | Are there any offers priced at zero? | DIRECT | Golden SQL evaluates `offer_price` |
| **T070** | on average, what percent discount do we give per booking? | DIRECT | Question asks for percent discount; trips on offer vs booking metric whitelist |
| **T080** | which offer gave the biggest discount on a single booking? | DIRECT | Golden SQL evaluates discount amounts |
| **T099** | Find shops where their actual price and coupon price are exactly the same. | DIRECT | Golden SQL compares `actual_price` and `coupon_price` |
| **T119** | what's the priciest deal on the app? | DIRECT | Golden SQL orders by `offer_price DESC` |
| **T123** | is there any deal that costs more than 60000 rupees? | DIRECT | Golden SQL compares `offer_price > 60000` |
| **T130** | looking for a cupboard, any deals going? | DIRECT | Golden SQL: `offer_price` |
| **T131** | which deal saves me the most money? | DIRECT | Golden SQL: orders by savings `(actual_price - offer_price) DESC` |
| **T133** | I want to learn coding, are there any courses on offer? | DIRECT | Golden SQL: `offer_price` |

- **Count:** 12 questions (12 DIRECT, 0 LIKELY)

---

### [H14] Test-Case Reference: "case #39" Unscoped Percentage
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:193-194`
- **Quote [VERIFIED]:**
  > `For an unscoped percentage, as in case #39, "percentage of reviews rated five stars" uses the same rating = 5 condition and where_expression: null.`
- **Affected Questions:**
*(None in `case_main.json`. Refers to case #39 from the earlier benchmark dataset `phase6_master_benchmark.csv`)*

- **Count:** 0 questions in `case_main.json`

---

### [H15] Counting Unique Users Joining Through Reviews
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:238-242`
- **Quote [VERIFIED]:**
  > `To count unique people (for example "how many unique users reviewed shops in X"), do NOT count a foreign-key column such as reviews.user_id. Join the people table through its verified relationship (reviews -> users), count DISTINCT users.id, and set count_entity to {"table": "users", "column": "id"}. Keep the measured table (reviews) as the base table so averages stay correct.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T005** | Who bought a premium plan and also left a 5-star review for a cafe in Trivandrum? | DIRECT | Golden SQL: `JOIN reviews r ON r.user_id = u.id` |
| **T017** | I want to see the names of people who booked an offer at 'Timepass' and left a review less than 3 stars. | DIRECT | Golden SQL: `JOIN reviews r ON r.user_id = u.id` |

- **Count:** 2 questions (2 DIRECT, 0 LIKELY)

---

### [H16] Category Left-Join Child Counting Instruction
- **Location:** `backend/app/agent/prompts/generate_query_prompt.py:324-336`
- **Quote [VERIFIED]:**
  > `For questions asking how many entities are in each dimension (for example, shops in each category or active shops across categories), use the dimension table as the base (e.g. categories) and LEFT JOIN the counted entity (e.g. shop_details) through a verified relationship. Filters on the joined child entity (such as shop_details.status = 1) MUST be placed inside that join's "conditions" array... Count the child's primary key (e.g. COUNT(shop_details.id)) so dimensions with zero matching entities appear as 0.`
- **Affected Questions:**

| Question ID | Question Text | Match Type | Evidence |
| :--- | :--- | :--- | :--- |
| **T061** | How many active shops are in each category? | DIRECT | Golden SQL: `categories c LEFT JOIN shop_details sd ... COUNT(sd.id)` |
| **T065** | Which shop categories get viewed the most? | DIRECT | Golden SQL: `categories c ... JOIN shop_details sd` |
| **T014** | Which category has the highest average booking amount? | DIRECT | Golden SQL: `categories c JOIN shop_details sd` |
| **T021** | Calculate the total revenue generated by each subcategory in the last 6 months. | DIRECT | Golden SQL: `subcategories sc JOIN shop_details sd` |
| **T090** | List the top 3 most commonly booked categories. | DIRECT | Golden SQL: `categories c JOIN shop_details sd` |
| **T112** | which type of business is most common on our app? | DIRECT | Golden SQL: `categories c JOIN shop_details sd` |

- **Count:** 6 questions (6 DIRECT, 0 LIKELY)

---

## 2. Questions Affected by Several Items

Many questions in `case_main.json` rely on 3 or more hardcoded prompt items simultaneously:

| Question ID | Question Text | Intersecting Prompt Items |
| :--- | :--- | :--- |
| **T003** | Are there any good restaurants in Colachel? | **H01** (Restaurent), **H09** (Rating policy), **H12** (is_default) |
| **T005** | Who bought a premium plan and also left a 5-star review for a cafe in Trivandrum? | **H08** (plan_id), **H09** (rating), **H12** (is_default), **H15** (reviews -> users) |
| **T012** | What's the cheapest offer available at any restaurant in Nagercoil right now? | **H01** (Restaurent), **H05** (Nagercoil), **H10** (offer status), **H12** (is_default), **H13** (metric whitelist) |
| **T014** | Which category has the highest average booking amount? | **H07** (category revenue), **H16** (category join) |
| **T017** | I want to see the names of people who booked an offer at 'Timepass' and left a review less than 3 stars. | **H09** (rating), **H15** (reviews -> users) |
| **T021** | Calculate the total revenue generated by each subcategory in the last 6 months. | **H07** (category revenue), **H11** (created_at), **H16** (subcategory join) |
| **T025** | Show me all active shops in Nagercoil. | **H05** (Nagercoil), **H06** (active shops in Nagercoil), **H12** (is_default) |
| **T067** | which shops in Trivandrum have the worst ratings? | **H09** (rating), **H12** (is_default) |
| **T070** | on average, what percent discount do we give per booking? | **H06** (percentage suppression), **H13** (discount metric whitelist) |
| **T090** | List the top 3 most commonly booked categories. | **H07** (category revenue), **H16** (category join) |
| **T112** | which type of business is most common on our app? | **H04** (Food & Dining), **H16** (category join) |
| **T126** | I'm thirsty, where can I get a juice in Nagercoil? | **H05** (Nagercoil), **H12** (is_default) |
| **T127** | craving biriyani, any place in Nagercoil? | **H05** (Nagercoil), **H10** (offer status), **H12** (is_default) |
| **T130** | looking for a cupboard, any deals going? | **H10** (offer status), **H13** (discount metric) |
| **T131** | which deal saves me the most money? | **H10** (offer status), **H13** (savings metric) |
| **T133** | I want to learn coding, are there any courses on offer? | **H10** (offer status), **H13** (offer price) |
| **T135** | any toy shops in Nagercoil for my kid's birthday? | **H05** (Nagercoil), **H10** (offer status), **H12** (is_default) |

---

## 3. Items That Affect Zero Questions in `case_main.json`

- **[H02]** `classifier_prompt.py:111-112`: `"car mechanic" -> "Bike & Car Servicing"` (Tested in legacy regression suites, not present in `case_main.json`).
- **[H14]** `generate_query_prompt.py:193-194`: `"as in case #39, 'percentage of reviews rated five stars'"` (Explicitly names case #39 from older benchmark files; not in `case_main.json`).

---

## 4. Re-Test Mapping (Item $\rightarrow$ Question IDs to Re-Run)

When any hardcoded item is removed or generalized, re-run exactly these test questions:

- **H01 (Restaurent typo):** `T003`, `T006`, `T012`, `T072`
- **H03 (Services category):** `T097`
- **H04 (Food & Dining category):** `T112`
- **H05 (Drinks, Nagercoil, Kochi examples):** `T012`, `T025`, `T066`, `T082`, `T087`, `T091`, `T126`, `T127`, `T128`, `T129`, `T132`, `T135`
- **H06 (Clarification suppression examples):** `T025`, `T070`
- **H07 (Category booking revenue traversal):** `T014`, `T021`, `T090`
- **H08 (Subscription status & plan_id):** `T005`, `T013`, `T018`, `T028`, `T029`, `T030`, `T035`, `T037`, `T040`, `T041`, `T044`, `T089`
- **H09 (Rating ranking policy & no minimum reviews):** `T003`, `T005`, `T010`, `T017`, `T019`, `T067`, `T068`, `T075`, `T085`, `T109`, `T114`, `T116`, `T124`
- **H10 (Active offer status = 1 & review counting):** `T008`, `T012`, `T020`, `T022`, `T027`, `T046`, `T047`, `T050`, `T051`, `T052`, `T053`, `T054`, `T056`, `T057`, `T059`, `T062`, `T098`, `T099`, `T101`, `T113`, `T118`, `T119`, `T122`, `T123`, `T125`, `T127`, `T130`, `T131`, `T133`, `T135`
- **H11 (Booking month vs created date):** `T002`, `T007`, `T021`, `T031`, `T043`, `T079`, `T086`, `T108`
- **H12 (shop_address.is_default = 1):** `T003`, `T005`, `T010`, `T012`, `T015`, `T016`, `T025`, `T066`, `T067`, `T071`, `T078`, `T081`, `T082`, `T087`, `T096`, `T110`, `T111`, `T126`, `T127`, `T128`, `T129`, `T132`, `T135`
- **H13 (Metric whitelist):** `T012`, `T050`, `T057`, `T059`, `T070`, `T080`, `T099`, `T119`, `T123`, `T130`, `T131`, `T133`
- **H15 (Reviews to users join order):** `T005`, `T017`
- **H16 (Category left-join child counting):** `T014`, `T021`, `T061`, `T065`, `T090`, `T112`
