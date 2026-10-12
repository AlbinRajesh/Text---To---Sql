**The big idea:** A user asks a question in plain English → 7 steps clean it up, ground it in real data, turn it into a safe MongoDB query, run it, and return an answer. Each file below is one of those steps.

### Files Built So Far

| File | What it does | Why it exists |
| :--- | :--- | :--- |
| `mongo_client.py` | Connects to MongoDB (read-only) | Nothing else works without a DB connection. Read-only = even if AI messes up, it can't delete/change data. |
| `business_rules.json` | A dictionary of company-specific term meanings (e.g., "active" = status is active AND not expired) | Stops the AI from guessing what business terms mean — it looks them up instead. |
| `sag_sampling.py` | Pulls a few real sample documents from MongoDB | MongoDB has no fixed structure, so the AI needs to see real field names, not guess them. |
| `llm_service.py` | The only file that talks to the Groq AI models | Centralizes AI calls — routes cheap questions to a cheap model, hard questions to a strong model. Saves cost. |
| `classifier_prompt.py` + `classifier_rewrite.py` (Node 1) | Reads the user's question, decides simple/complex, cleans up vague wording | First step — turns messy human questions into clear ones before anything else happens. |
| `business_rules.py` (Node 5) | Looks up which rules from `business_rules.json` apply to this specific question | Connects the rulebook (data) to the actual question being asked right now. |
| `generate_query_prompt.py` + `generate_query.py` (Node 6) | The AI writes the actual MongoDB query, using real sample fields + matched business rules | The "brain" step — this is what actually decides what to search for. |
| `safety_check.py` (Node 7) | Checks the AI-written query for anything dangerous before it runs | Second/third layer of protection — blocks harmful operations even if the AI made a mistake. |
| `run_self_fix.py` (Node 8, basic) | Actually runs the safe query on MongoDB and returns results | Executes the final approved query and hands back the answer. |

### How They Connect (The Flow)

**User question**
↓
**Node 1 (`classifier_rewrite.py`)** → Cleaned-up question
↓
**`sag_sampling.py`** → Real sample data from DB
↓
**Node 5 (`business_rules.py`)** → Matched company rules
↓
**Node 6 (`generate_query.py`)** → AI writes MongoDB query (uses sample data + rules)
↓
**Node 7 (`safety_check.py`)** → Checks query is safe, adds limits
↓
**Node 8 (`run_self_fix.py`)** → Runs query, returns results

### What's NOT Done Yet

These files work individually, but nothing connects them into one automatic flow yet. Right now you could test each file on its own — there's no single "ask a question, get an answer" button. That connecting piece (`graph.py` + `state.py` + the API endpoint) is the next real milestone.


# <REDACTED: credential removed>
# groq_ai=<REDACTED>



nosql_agent\
├── .gitignore
├── overview.md
├── package-lock.json
├── Project_info.md
├── README.md
├── rules.md
├── worklog.md
├── backend\
│   ├── .env
│   ├── requirements.txt
│   └── app\
│       ├── __init__.py
│       ├── main.py
│       ├── agent\
│       │   ├── __init__.py
│       │   ├── graph.py
│       │   ├── state.py
│       │   ├── nodes\
│       │   │   ├── __init__.py
│       │   │   ├── business_rules.py
│       │   │   ├── cache_check.py
│       │   │   ├── classifier_rewrite.py
│       │   │   ├── escalation.py
│       │   │   ├── generate_query.py
│       │   │   ├── grade_sample.py
│       │   │   ├── result_grader.py
│       │   │   ├── return_save.py
│       │   │   ├── run_self_fix.py
│       │   │   ├── safety_check.py
│       │   │   └── schema_grounding.py
│       │   └── prompts\
│       │       ├── __init__.py
│       │       ├── classifier_prompt.py
│       │       ├── generate_query_prompt.py
│       │       ├── grader_prompt.py
│       │       └── repair_prompt.py
│       ├── api\
│       │   ├── __init__.py
│       │   └── routes\
│       │       ├── __init__.py
│       │       ├── chat.py
│       │       └── health.py
│       ├── db\
│       │   ├── __init__.py
│       │   ├── cache_store.py
│       │   ├── mongo_client.py
│       │   └── mysql_client.py
│       ├── models\
│       │   ├── __init__.py
│       │   └── chat.py
│       ├── rules\
│       │   └── business_rules.json
│       ├── services\
│       │   ├── __init__.py
│       │   ├── embedding_service.py
│       │   └── llm_service.py
│       ├── tests\
│       │   ├── __init__.py
│       │   ├── question_bank.json
│       │   ├── test_business_rules.py
│       │   ├── test_classifier_rewrite.py
│       │   ├── test_generate_query.py
│       │   ├── test_grade_sample.py
│       │   ├── test_mysql_client.py
│       │   ├── test_run_self_fix.py
│       │   ├── test_safety_check.py
│       │   ├── test_schema_grounding.py
│       │   └── eval\
│       │       ├── __init__.py
│       │       ├── llm_judge.py
│       │       └── run_eval.py
│       └── utils\
│           ├── __init__.py
│           ├── audit.py
│           └── logging.py
├── docs\
│   └── approach_note.md
└── frontend\
    ├── .gitignore
    ├── eslint.config.js
    ├── index.html
    ├── package.json
    ├── package-lock.json
    ├── README.md
    ├── vite.config.js
    ├── public\
    │   ├── favicon.svg
    │   └── icons.svg
    └── src\
        ├── App.css
        ├── App.jsx
        ├── index.css
        ├── main.jsx
        ├── api\
        │   └── chatApi.js
        ├── assets\
        │   ├── hero.png
        │   ├── react.svg
        │   └── vite.svg
        ├── components\
        │   ├── ChatWindow.jsx
        │   ├── MessageBubble.jsx
        │   ├── QueryResultTable.jsx
        │   └── Sidebar.jsx
        ├── hooks\
        │   └── useChat.js
        └── pages\
            └── ChatPage.jsx


Assessing whether the codebase follows a modular structure.
Assessing whether the codebase follows a modular structure.
Yes, it's modular. Here's what that means in your case, with examples:

1. Each node = one file, one job.
grade_sample.py only grades schema sufficiency. safety_check.py only validates SQL. result_grader.py only checks if a result answers the question. None of them know about each other — safety_check.py never imports grade_sample.py. That's the core of "modular": each piece does one thing and doesn't need to know how the others work internally.

2. Shared contracts, not shared internals.
Nodes don't call each other directly — they all just read/write to the same GraphState dict, and graph.py is the only place that wires them together in order. Example: node_result_grader doesn't call node_execute — it just reads state["execution_result"], which node_execute already put there. If you rewrote run_self_fix.py completely tomorrow, result_grader.py wouldn't need to change at all, as long as it still produces the same shape of execution_result.

3. Cross-cutting stuff is centralized, not copy-pasted.
LLM calls all go through one place — llm_service.py's get_llm_service(). Every node that needs an LLM (grade_sample, generate_query, result_grader) calls the same call_agent_json("ROLE", ...) pattern instead of each writing its own Groq/Gemini client code. Same for MySQL — one mysql_client.py, everyone else just calls fetch_all_readonly().

4. Each module owns its own errors.
GradeSampleError, SafetyViolationError, QueryExecutionError, ResultGraderError — each file defines its own exception type. graph.py catches each one by name and decides what to do. This means a node can fail loudly and specifically, and the orchestrator (graph.py) is the only place that has to know "what do I do when X breaks."

Two small rough edges, worth a quick cleanup pass — not urgent, but real:

test_graph.py imports refresh_schema_cache and SchemaGroundingError twice, from the same module, in two separate lines. Harmless (Python just re-binds the same name), but it's dead weight — delete the duplicate line.
Several nodes do from app.services.llm_service import get_llm_service inside the function, not at the top of the file. This is usually done on purpose to avoid circular imports, so it's not automatically wrong — but check if that's actually why it's done here, or if it's just leftover from earlier iteration. If there's no circular import risk, move those to the top for consistency.
Overall: yes, this is a legitimately modular structure for a system this size. The node/service/db separation is the right shape for an enterprise pipeline like this.


-----------------------------------------------------------------------------------------
some questions from the database
Test questions, by join depth

Simple (1 table)

How many users have status = 1?
What is the average rating in reviews?

Moderate (2 tables)
3. List the shop names and their average rating from reviews.
4. How many bookings does each shop have? (edge case: shops with zero bookings should show 0, not be missing.)

Complex (3 to 4 tables)
5. List shop names, their category name, and their average review rating.
6. For each category, how many distinct users have left a review on a shop in that category?
7. List users who have an active subscription and their plan's price. (edge case: the plan_id type mismatch above may make this return nothing — good test of that gotcha.)

Complex (5 tables)
8. For each category, show the category name, number of shops, number of offers, and number of bookings across all shops in that category.
9. List users with more than one booking, along with the shop name and offer heading for their most recent booking.

Edge cases to test on their own
10. Show reviews for a shop that has none. Should return an empty result, not an error.
11. "How many shops have a rating above 4.5?" (Correct handling of shops with zero reviews — should they count as excluded, or as having no rating? Worth deciding and checking what the SQL actually does.)
12. A question naming a shop that doesn't exist, e.g. "Show reviews for shop 99999." Should come back as "no matching records," not escalate.

Run questions 5 to 9 through your own SQL first and compare to the app's answer, the same way you checked the earlier ones.


DB__DETAILS------------------------------------------------------------------------------------

# Coupown Database (`coupown_db`) Documentation

**Database Engine:** MySQL  
**Agent Rule Version:** 1.2.0 (Last Updated: 2026-09-29)  
**Total Tables:** 49  

---

## 1. Table Directory (49 Tables)
The database contains the following tables. Only a subset has been fully mapped and utilized by the AI agent so far.

`active_logins`, `advertisements`, `advertisements_defualt`, `ai_image`, `bookings`, `cache`, `cache_locks`, `callviews`, `categories`, `coupon_purchase_payment`, `coupon_usages`, `daily_user_counts`, `editable_offers_view`, `failed_jobs`, `favourite`, `golden_coupon`, `invoices`, `job_batches`, `jobs`, `jobsdata`, `likes`, `login_logs`, `migration_lock`, `migrations`, `notification_data`, `oauth_access_tokens`, `oauth_auth_codes`, `oauth_clients`, `oauth_personal_access_clients`, `oauth_refresh_tokens`, `offer`, `otps`, `password_reset_tokens`, `personal_access_tokens`, `plans`, `products`, `razorpay_subscriptions`, `reviews`, `service`, `sessions`, `shop_address`, `shop_details`, `subcategories`, `subscriptions`, `typesense_sync_queue`, `user_address`, `user_locations`, `users`, `views`.

---

## 2. Known Table Schemas

### `shop_details` (Core Entity Table)
One row represents a single shop/vendor.
| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | `int` | **Primary Key** |
| `partner_id` | `int` | |
| `shoplogo` | `text` | |
| `name` | `text` | Shop Name |
| `phone` | `text` | Contact Number |
| `websitelink` | `text` | |
| `category_id` | `int` | FK to `categories.id` |
| `subcategory_id` | `int` | FK to `subcategories.id` |
| `status` | `tinyint(1)` | Default `1` (Active) |
| `licence` | `varchar(200)`| |
| `insurance` | `varchar(200)`| |
| `rcbook` | `varchar(200)`| |
| `vechicle_no` | `varchar(200)`| |
| `no_of_sheet` | `int` | |
| `created_at` | `text` | |
| `updated_at` | `text` | |

### `categories`
Top-level classification for shops.
| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | `int` | **Primary Key** (e.g., 2 = 'Food & Dining') |
| `categoriesname` | `varchar` | Category label |

### `subcategories`
Finer classification under each top-level category.
| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | `bigint` | **Primary Key** (e.g., 9 = 'Restaurent') |
| `subcategory_id` | `int` | |
| `category_id` | `bigint` | FK to `categories.id` |
| `subcategoryname` | `varchar(255)`| Note: Restaurant is misspelled as "Restaurent" in DB |
| `subcat_img` | `text` | |
| `visible_status` | `tinyint(1)` | |
| `type` | `int` | |
| `created_at` | `timestamp` | |
| `updated_at` | `timestamp` | |

### `offer` (Inferred Structure)
| Column | Notes |
| :--- | :--- |
| `id` | **Primary Key** |
| `shop_id` | FK to `shop_details.id` |
| `offer_heading` | Title of the offer |
| `actual_price` | Price before discount |
| `offer_price` | Price after discount (Max recorded: 200,000) |
| `start_date` | Date |
| `end_date` | Date (Used for expiration logic) |
| `status` | e.g., 'expired' |

### `bookings` (Inferred Structure)
| Column | Notes |
| :--- | :--- |
| `id` | **Primary Key** |
| `shop_id` | FK to `shop_details.id` |
| `user_id` | FK to `users.id` |
| `final_amount` | Amount paid after discounts (Defined as "booking amount") |
| `actual_amount`| Price before discount |
| `city` | e.g., 'Nagercoil', 'Thiruvananthapuram' |
| `status` | |
| `created_at` | Timestamp |

### `users` (Inferred Structure)
| Column | Notes |
| :--- | :--- |
| `id` | **Primary Key** |
| `plan_id` | FK to `plans.id` (Can be NULL) |

### `subscriptions` (Inferred Structure)
| Column | Notes |
| :--- | :--- |
| `id` | **Primary Key** |
| `customer_id` | Should map to `users.id`, but *currently unverified in schema grounding*. |
| `status` | e.g., 'active', 'cancelled' |
| `amount` | Subscription cost |

### `plans` & `products` (Partial/Isolated Tables)
*   **`plans`**: Contains `id`, `name` (e.g., 'Premium Plan'), and `price` (Max value 999.00).
*   **`products`**: Contains `id`, `product_name`, `user_id`, `created_at`, `updated_at`. *(Note: Not linked to shops. Cannot be used to answer "What food does this restaurant sell".)*

---

## 3. Verified Relationships (Agent-Approved Joins)
The AI agent relies on a strict schema grounding list to prevent hallucinations. The following 7 relationships are explicitly verified (`reviewed_manual_query`):

1.  **`reviewed_bookings_shop_id`**: `bookings.shop_id` -> `shop_details.id`
2.  **`reviewed_offer_shop_id`**: `offer.shop_id` -> `shop_details.id`
3.  **`reviewed_reviews_shop_id`**: `reviews.shop_id` -> `shop_details.id`
4.  **`reviewed_reviews_user_id`**: `reviews.user_id` -> `users.id`
5.  **`reviewed_shop_details_category_id`**: `shop_details.category_id` -> `categories.id`
6.  **`reviewed_shop_details_subcategory_id`**: `shop_details.subcategory_id` -> `subcategories.id`
7.  **`subcategories_category_id_foreign`**: `subcategories.category_id` -> `categories.id`

### ⚠️ Missing / Unverified Configurations
The agent will intentionally generate a `UnanswerableQuestionError` (Safe Refusal) if asked about these, to protect against bad data logic:
*   **`subscriptions` to `users`**: No verified relationship exists between `subscriptions.customer_id` and `users.id`.
*   **"Active" / "Expired" Dates**: Relative dates cannot be reliably interpreted until a timezone and calendar policy are configured in the business rules. Explicit dates (e.g., `> '2026-01-01'`) must be used instead.

---

## 4. Business Rules (`app/rules/business_rules.json`)
The query generation pipeline uses an injected semantic layer to translate user terms into exact mathematical formulas. 

### A. Review Ratings (Average Thresholds)
Handled via `reviews` joined to `shop_details`. Aggregations use `AVG(rating)`, dropping NULLs.
*   **`shops_above_average_rating_threshold`**: Count shops where `AVG(rating) > X`.
*   **`list_shops_above_average_rating_threshold`**: List shops where `AVG(rating) > X`.
*   **`shops_at_least_average_rating_threshold`**: Count shops where `AVG(rating) >= X`.
*   **`list_shops_at_least_average_rating_threshold`**: List shops where `AVG(rating) >= X`.
*   **`shops_below_average_rating_threshold`**: Count shops where `AVG(rating) < X`.
*   **`list_shops_below_average_rating_threshold`**: List shops where `AVG(rating) < X`.

### B. Financial Definitions
*   **`booking_amount`**: "Booking amount" explicitly maps to `bookings.final_amount` (the amount actually paid after discounts). The agent is strictly instructed *not* to use `bookings.actual_amount` unless the user specifies "actual" or "original" amount.

---

## 5. Known Database Facts & Statistics
Derived from raw SQL executions during benchmarking:

**Entity Counts:**
*   **Users:** 2,559 total (2,375 have no plan assigned).
*   **Shops:** 206 total (198 have zero bookings). Only 15 have a populated website link.
*   **Offers:** 194 active, 127 expired.
*   **Subscriptions:** 195 active, 0 cancelled.
*   **Categories:** 15 total top-level categories.

**Category Breakdowns (Category ID -> Shop Count):**
*   2 (Food & Dining) -> 96 shops
*   13 (Transportation Services) -> 30 shops
*   12 (Services) -> 29 shops
*   5 (Mobiles & Electronics) -> 11 shops
*   15 (Others) -> 10 shops
*   1 (Fashions) -> 9 shops
*   3 (Home & Living Furnitures) -> 9 shops
*   11 (Automobiles) -> 5 shops
*   7 (Grocery, Beauty & Health) -> 3 shops
*   14 (Travel & Hospitality) -> 2 shops
*   4 (Electrical Appliances) -> 1 shop
*   8 (Entertainment) -> 1 shop
*   *Zero Counts:* Books & Stationery, Sports Products, Gifts & Jewels.

**Notable Records:**
*   Highest Total Booking Value: *Boss App Studio Pvt Ltd* (82,191).
*   Highest Offer Price Listed:  200,000.
*   Premium Plan Price: 999.00.
*   Average Booking Final Amount (Global): 3,601.88.
*   Nagercoil Bookings: 4 total (Average amount: 850.00).



-----------------------------------------------------------------------------------------------
TEST QUERYS-------------------------------------
Category 1: Location & Geospatial Analytics
1. Question: What is the full address of the highest-rated shop in Kazhakkoottam?

SQL Query:

SQL
SELECT a.buildingnumber, a.arearoadname, a.nearbylandmark, a.city, a.state, a.pincode 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
JOIN reviews r ON s.id = r.shop_id 
WHERE a.city = 'Kazhakkoottam' 
GROUP BY s.id, a.buildingnumber, a.arearoadname, a.nearbylandmark, a.city, a.state, a.pincode 
ORDER BY AVG(r.rating) DESC 
LIMIT 1;
Answer: Module 22, Technopark, Near TCS, Kazhakkoottam, Kerala - 695581 (Shop ID 1).

2. Question: List the names and landmarks of shops in Tamil Nadu offering a discount of more than 500 rupees.

SQL Query:

SQL
SELECT DISTINCT s.name, a.nearbylandmark 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
JOIN offer o ON s.id = o.shop_id 
WHERE a.state = 'Tamil Nadu' 
  AND (o.actual_price - o.offer_price) > 500;
Answer: Shops in Tamil Nadu whose active discounts exceed 500 (such as offers reduced from 9,999 to 8,999).

3. Question: Which shop in Kerala has the highest total booking revenue and an average rating above 4.0?

SQL Query:

SQL
SELECT s.name, SUM(b.final_amount) AS revenue 
FROM bookings b 
JOIN shop_details s ON b.shop_id = s.id 
JOIN shop_address a ON s.id = a.shop_id 
JOIN reviews r ON s.id = r.shop_id 
WHERE a.state = 'Kerala' 
GROUP BY s.id, s.name 
HAVING AVG(r.rating) > 4.0 
ORDER BY revenue DESC 
LIMIT 1;
Answer: The top revenue-generating reviewed shop located in Kerala.

4. Question: Give me the phone number and area road name of the shop that has the 'Lucky Users Special Offer!'.

SQL Query:

SQL
SELECT s.name, s.phone, a.arearoadname 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
JOIN offer o ON s.id = o.shop_id 
WHERE o.offer_heading = 'Lucky Users Special Offer!' 
LIMIT 1;
Answer: Returns the phone number and street/road location of the shop offering that promotion.

5. Question: How many shops located in Perumselvavilai have zero bookings?

SQL Query:

SQL
SELECT COUNT(DISTINCT s.id) 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
LEFT JOIN bookings b ON s.id = b.shop_id 
WHERE a.city = 'Perumselvavilai' 
  AND b.id IS NULL;
Answer: Total count of registered shops located in Perumselvavilai with no booking records.

Category 2: Deep Hierarchies & Text Matching
6. Question: Find the average booking value for shops in the 'Restaurent' subcategory.

SQL Query:

SQL
SELECT ROUND(AVG(b.final_amount), 2) 
FROM bookings b 
JOIN shop_details s ON b.shop_id = s.id 
JOIN subcategories sc ON s.subcategory_id = sc.id 
WHERE sc.subcategoryname = 'Restaurent';
Answer: The average final amount paid across all bookings for shops assigned to subcategory ID 9 ("Restaurent").

7. Question: List the top 3 categories by total discount value offered right now.

SQL Query:

SQL
SELECT c.categoriesname, SUM(o.actual_price - o.offer_price) AS total_savings 
FROM categories c 
JOIN shop_details s ON c.id = s.category_id 
JOIN offer o ON s.id = o.shop_id 
GROUP BY c.id, c.categoriesname 
ORDER BY total_savings DESC 
LIMIT 3;
Answer: The top three categories with the largest cumulative price reductions across their active offers.

8. Question: Which city has the lowest average rating for shops in the 'Transportation Services' category?

SQL Query:

SQL
SELECT a.city, AVG(r.rating) AS avg_rating 
FROM shop_address a 
JOIN shop_details s ON a.shop_id = s.id 
JOIN categories c ON s.category_id = c.id 
JOIN reviews r ON s.id = r.shop_id 
WHERE c.categoriesname = 'Transportation Services' 
GROUP BY a.city 
ORDER BY avg_rating ASC 
LIMIT 1;
Answer: The city whose transportation shops have received the lowest overall customer review ratings.

9. Question: Find the highest-rated shop that offers 'குறைந்த விலையில் மரங்கள் வெட்டி தர படும்'.

SQL Query:

SQL
SELECT s.name, AVG(r.rating) AS avg_rating 
FROM shop_details s 
JOIN offer o ON s.id = o.shop_id 
JOIN reviews r ON s.id = r.shop_id 
WHERE o.offer_heading = 'குறைந்த விலையில் மரங்கள் வெட்டி தர படும்' 
GROUP BY s.id, s.name 
ORDER BY avg_rating DESC 
LIMIT 1;
Answer: The top-rated shop providing that specific tree-cutting offer.

10. Question: List the names and phone numbers of shops in the 'Food & Dining' category that have at least 3 offers.

SQL Query:

SQL
SELECT s.name, s.phone, COUNT(o.id) AS offer_count 
FROM shop_details s 
JOIN categories c ON s.category_id = c.id 
JOIN offer o ON s.id = o.shop_id 
WHERE c.categoriesname = 'Food & Dining' 
GROUP BY s.id, s.name, s.phone 
HAVING COUNT(o.id) >= 3;
Answer: List of food outlets with 3 or more offers (e.g., Kingstar Fresh Juice Point, Annai Fish Pickles).

Category 3: Edge Cases & Semantic Rules
11. Question: Find the top 3 categories that generated the highest total booking revenue.

SQL Query:

SQL
SELECT c.categoriesname, SUM(b.final_amount) AS revenue 
FROM categories c 
JOIN shop_details s ON c.id = s.category_id 
JOIN bookings b ON s.id = b.shop_id 
GROUP BY c.id, c.categoriesname 
ORDER BY revenue DESC 
LIMIT 3;
Answer: The top three revenue-generating categories using actual amounts paid after discounts (final_amount).

12. Question: Give me a breakdown of cancelled subscriptions: how many are there in total, and what is the total amount lost from them?

SQL Query:

SQL
SELECT COUNT(*) AS total_cancelled, COALESCE(SUM(amount), 0) AS total_lost 
FROM subscriptions 
WHERE status = 'cancelled';
Answer: Total cancelled: 0, Total amount lost: 0.

13. Question: How many users do not have any plan assigned to them and have never made a booking?

SQL Query:

SQL
SELECT COUNT(DISTINCT u.id) 
FROM users u 
LEFT JOIN bookings b ON u.id = b.user_id 
WHERE u.plan_id IS NULL 
  AND b.id IS NULL;
Answer: The count of registered users without a plan_id who have no records in the bookings table.

14. Question: Are there any top-level categories that have absolutely zero offers posted across all their shops?

SQL Query:

SQL
SELECT c.categoriesname 
FROM categories c 
LEFT JOIN shop_details s ON c.id = s.category_id 
LEFT JOIN offer o ON s.id = o.shop_id 
GROUP BY c.id, c.categoriesname 
HAVING COUNT(o.id) = 0;
Answer: Books & Stationery, Sports Products, Gifts & Jewels.

15. Question: Which subcategory has the highest average review rating, and how many unique users have left a review for shops in that subcategory?

SQL Query:

SQL
SELECT sc.subcategoryname, AVG(r.rating) AS avg_rating, COUNT(DISTINCT r.user_id) AS reviewer_count 
FROM subcategories sc 
JOIN shop_details s ON sc.id = s.subcategory_id 
JOIN reviews r ON s.id = r.shop_id 
GROUP BY sc.id, sc.subcategoryname 
ORDER BY avg_rating DESC 
LIMIT 1;
Answer: The highest-rated subcategory along with its count of unique review authors.

Category 4: Safety & Missing Relationship Checks
16. Question: List the names of shops that have an average review rating of at least 4.0 and also have a total booking revenue greater than 5000.

SQL Query: Requires safe query structuring or subqueries to avoid multiplying booking totals by review counts.

Answer: Refusal / Safety Block (Joined numeric aggregates must measure the base table and join only to its many-to-one parents).

17. Question: Show me the products available at the highest-rated restaurant in Nagercoil.

SQL Query: None (Unanswerable).

Answer: Refusal ("This question requires a verified relationship between shops and products, which is not currently configured").

18. Question: List the names of users who have booked a shop in the 'Fashions' category and also hold an active subscription.

SQL Query: None (Unanswerable).

Answer: Refusal ("This question requires a verified relationship between users and subscriptions, which is not currently configured").

19. Question: What is the total value of currently active offers?

SQL Query: None (Unanswerable).

Answer: Refusal ("Relative dates cannot be interpreted reliably until a timezone and calendar policy are configured").

20. Question: Show the shop name and pincode for all shops that have a vehicle number listed in their details.

SQL Query:

SQL
SELECT s.name, a.pincode 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
WHERE s.vechicle_no IS NOT NULL 
  AND s.vechicle_no != '';
Answer: Names and pincodes for shops with vehicle registration numbers listed.

Category 5: Multi-Condition Filtering & Complex Joins
21. Question: Which category has the highest number of shops physically located in 'Thiruvananthapuram'?

SQL Query:

SQL
SELECT c.categoriesname, COUNT(DISTINCT s.id) AS shop_count 
FROM categories c 
JOIN shop_details s ON c.id = s.category_id 
JOIN shop_address a ON s.id = a.shop_id 
WHERE a.city = 'Thiruvananthapuram' 
GROUP BY c.id, c.categoriesname 
ORDER BY shop_count DESC 
LIMIT 1;
Answer: The top business category in Thiruvananthapuram by number of shops.

22. Question: List the names of shops in the 'Services' category that have a verified default address and are currently active.

SQL Query:

SQL
SELECT s.name 
FROM shop_details s 
JOIN categories c ON s.category_id = c.id 
JOIN shop_address a ON s.id = a.shop_id 
WHERE c.categoriesname = 'Services' 
  AND s.status = 1 
  AND a.is_default = 1;
Answer: Names of active service-category shops where is_default = 1.

23. Question: What is the average discounted offer price for shops that have the word 'Juice' in their name?

SQL Query:

SQL
SELECT ROUND(AVG(o.offer_price), 2) AS avg_juice_offer 
FROM offer o 
JOIN shop_details s ON o.shop_id = s.id 
WHERE s.name LIKE '%Juice%';
Answer: The average offer_price across shops containing "Juice" in their name.

24. Question: Find the average review rating for products listed by shops in Kerala.

SQL Query: None (Unanswerable).

Answer: Refusal ("This question requires a verified relationship between shops and products, which is not currently configured").

25. Question: List the shop name, category name, and average review rating for all shops in the 'Fashions' category, ordered from highest to lowest rating.

SQL Query:

SQL
SELECT s.name, c.categoriesname, AVG(r.rating) AS avg_rating 
FROM shop_details s 
JOIN categories c ON s.category_id = c.id 
JOIN reviews r ON s.id = r.shop_id 
WHERE c.categoriesname = 'Fashions' 
GROUP BY s.id, s.name, c.id, c.categoriesname 
ORDER BY avg_rating DESC;
Answer: Ranked list of shops in the Fashions category based on their calculated average review rating.
--------------------------------------------------------------------------------------------
ADVANCED---
Category 1: Location & Geospatial Analytics
1. Question: What is the full address of the highest-rated shop in Kazhakkoottam?

SQL Query:

SQL
SELECT a.buildingnumber, a.arearoadname, a.nearbylandmark, a.city, a.state, a.pincode 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
JOIN reviews r ON s.id = r.shop_id 
WHERE a.city = 'Kazhakkoottam' 
GROUP BY s.id, a.buildingnumber, a.arearoadname, a.nearbylandmark, a.city, a.state, a.pincode 
ORDER BY AVG(r.rating) DESC 
LIMIT 1;
Answer: Module 22, Technopark, Near TCS, Kazhakkoottam, Kerala - 695581 (Shop ID 1).

2. Question: List the names and landmarks of shops in Tamil Nadu offering a discount of more than 500 rupees.

SQL Query:

SQL
SELECT DISTINCT s.name, a.nearbylandmark 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
JOIN offer o ON s.id = o.shop_id 
WHERE a.state = 'Tamil Nadu' 
  AND (o.actual_price - o.offer_price) > 500;
Answer: Shops in Tamil Nadu whose active discounts exceed 500 (such as offers reduced from 9,999 to 8,999).

3. Question: Which shop in Kerala has the highest total booking revenue and an average rating above 4.0?

SQL Query:

SQL
SELECT s.name, SUM(b.final_amount) AS revenue 
FROM bookings b 
JOIN shop_details s ON b.shop_id = s.id 
JOIN shop_address a ON s.id = a.shop_id 
JOIN reviews r ON s.id = r.shop_id 
WHERE a.state = 'Kerala' 
GROUP BY s.id, s.name 
HAVING AVG(r.rating) > 4.0 
ORDER BY revenue DESC 
LIMIT 1;
Answer: The top revenue-generating reviewed shop located in Kerala.

4. Question: Give me the phone number and area road name of the shop that has the 'Lucky Users Special Offer!'.

SQL Query:

SQL
SELECT s.name, s.phone, a.arearoadname 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
JOIN offer o ON s.id = o.shop_id 
WHERE o.offer_heading = 'Lucky Users Special Offer!' 
LIMIT 1;
Answer: Returns the phone number and street/road location of the shop offering that promotion.

5. Question: How many shops located in Perumselvavilai have zero bookings?

SQL Query:

SQL
SELECT COUNT(DISTINCT s.id) 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
LEFT JOIN bookings b ON s.id = b.shop_id 
WHERE a.city = 'Perumselvavilai' 
  AND b.id IS NULL;
Answer: Total count of registered shops located in Perumselvavilai with no booking records.

Category 2: Deep Hierarchies & Text Matching
6. Question: Find the average booking value for shops in the 'Restaurent' subcategory.

SQL Query:

SQL
SELECT ROUND(AVG(b.final_amount), 2) 
FROM bookings b 
JOIN shop_details s ON b.shop_id = s.id 
JOIN subcategories sc ON s.subcategory_id = sc.id 
WHERE sc.subcategoryname = 'Restaurent';
Answer: The average final amount paid across all bookings for shops assigned to subcategory ID 9 ("Restaurent").

7. Question: List the top 3 categories by total discount value offered right now.

SQL Query:

SQL
SELECT c.categoriesname, SUM(o.actual_price - o.offer_price) AS total_savings 
FROM categories c 
JOIN shop_details s ON c.id = s.category_id 
JOIN offer o ON s.id = o.shop_id 
GROUP BY c.id, c.categoriesname 
ORDER BY total_savings DESC 
LIMIT 3;
Answer: The top three categories with the largest cumulative price reductions across their active offers.

8. Question: Which city has the lowest average rating for shops in the 'Transportation Services' category?

SQL Query:

SQL
SELECT a.city, AVG(r.rating) AS avg_rating 
FROM shop_address a 
JOIN shop_details s ON a.shop_id = s.id 
JOIN categories c ON s.category_id = c.id 
JOIN reviews r ON s.id = r.shop_id 
WHERE c.categoriesname = 'Transportation Services' 
GROUP BY a.city 
ORDER BY avg_rating ASC 
LIMIT 1;
Answer: The city whose transportation shops have received the lowest overall customer review ratings.

9. Question: Find the highest-rated shop that offers 'குறைந்த விலையில் மரங்கள் வெட்டி தர படும்'.

SQL Query:

SQL
SELECT s.name, AVG(r.rating) AS avg_rating 
FROM shop_details s 
JOIN offer o ON s.id = o.shop_id 
JOIN reviews r ON s.id = r.shop_id 
WHERE o.offer_heading = 'குறைந்த விலையில் மரங்கள் வெட்டி தர படும்' 
GROUP BY s.id, s.name 
ORDER BY avg_rating DESC 
LIMIT 1;
Answer: The top-rated shop providing that specific tree-cutting offer.

10. Question: List the names and phone numbers of shops in the 'Food & Dining' category that have at least 3 offers.

SQL Query:

SQL
SELECT s.name, s.phone, COUNT(o.id) AS offer_count 
FROM shop_details s 
JOIN categories c ON s.category_id = c.id 
JOIN offer o ON s.id = o.shop_id 
WHERE c.categoriesname = 'Food & Dining' 
GROUP BY s.id, s.name, s.phone 
HAVING COUNT(o.id) >= 3;
Answer: List of food outlets with 3 or more offers (e.g., Kingstar Fresh Juice Point, Annai Fish Pickles).

Category 3: Edge Cases & Semantic Rules
11. Question: Find the top 3 categories that generated the highest total booking revenue.

SQL Query:

SQL
SELECT c.categoriesname, SUM(b.final_amount) AS revenue 
FROM categories c 
JOIN shop_details s ON c.id = s.category_id 
JOIN bookings b ON s.id = b.shop_id 
GROUP BY c.id, c.categoriesname 
ORDER BY revenue DESC 
LIMIT 3;
Answer: The top three revenue-generating categories using actual amounts paid after discounts (final_amount).

12. Question: Give me a breakdown of cancelled subscriptions: how many are there in total, and what is the total amount lost from them?

SQL Query:

SQL
SELECT COUNT(*) AS total_cancelled, COALESCE(SUM(amount), 0) AS total_lost 
FROM subscriptions 
WHERE status = 'cancelled';
Answer: Total cancelled: 0, Total amount lost: 0.

13. Question: How many users do not have any plan assigned to them and have never made a booking?

SQL Query:

SQL
SELECT COUNT(DISTINCT u.id) 
FROM users u 
LEFT JOIN bookings b ON u.id = b.user_id 
WHERE u.plan_id IS NULL 
  AND b.id IS NULL;
Answer: The count of registered users without a plan_id who have no records in the bookings table.

14. Question: Are there any top-level categories that have absolutely zero offers posted across all their shops?

SQL Query:

SQL
SELECT c.categoriesname 
FROM categories c 
LEFT JOIN shop_details s ON c.id = s.category_id 
LEFT JOIN offer o ON s.id = o.shop_id 
GROUP BY c.id, c.categoriesname 
HAVING COUNT(o.id) = 0;
Answer: Books & Stationery, Sports Products, Gifts & Jewels.

15. Question: Which subcategory has the highest average review rating, and how many unique users have left a review for shops in that subcategory?

SQL Query:

SQL
SELECT sc.subcategoryname, AVG(r.rating) AS avg_rating, COUNT(DISTINCT r.user_id) AS reviewer_count 
FROM subcategories sc 
JOIN shop_details s ON sc.id = s.subcategory_id 
JOIN reviews r ON s.id = r.shop_id 
GROUP BY sc.id, sc.subcategoryname 
ORDER BY avg_rating DESC 
LIMIT 1;
Answer: The highest-rated subcategory along with its count of unique review authors.

Category 4: Safety & Missing Relationship Checks
16. Question: List the names of shops that have an average review rating of at least 4.0 and also have a total booking revenue greater than 5000.

SQL Query: Requires safe query structuring or subqueries to avoid multiplying booking totals by review counts.

Answer: Refusal / Safety Block (Joined numeric aggregates must measure the base table and join only to its many-to-one parents).

17. Question: Show me the products available at the highest-rated restaurant in Nagercoil.

SQL Query: None (Unanswerable).

Answer: Refusal ("This question requires a verified relationship between shops and products, which is not currently configured").

18. Question: List the names of users who have booked a shop in the 'Fashions' category and also hold an active subscription.

SQL Query: None (Unanswerable).

Answer: Refusal ("This question requires a verified relationship between users and subscriptions, which is not currently configured").

19. Question: What is the total value of currently active offers?

SQL Query: None (Unanswerable).

Answer: Refusal ("Relative dates cannot be interpreted reliably until a timezone and calendar policy are configured").

20. Question: Show the shop name and pincode for all shops that have a vehicle number listed in their details.

SQL Query:

SQL
SELECT s.name, a.pincode 
FROM shop_details s 
JOIN shop_address a ON s.id = a.shop_id 
WHERE s.vechicle_no IS NOT NULL 
  AND s.vechicle_no != '';
Answer: Names and pincodes for shops with vehicle registration numbers listed.

Category 5: Multi-Condition Filtering & Complex Joins
21. Question: Which category has the highest number of shops physically located in 'Thiruvananthapuram'?

SQL Query:

SQL
SELECT c.categoriesname, COUNT(DISTINCT s.id) AS shop_count 
FROM categories c 
JOIN shop_details s ON c.id = s.category_id 
JOIN shop_address a ON s.id = a.shop_id 
WHERE a.city = 'Thiruvananthapuram' 
GROUP BY c.id, c.categoriesname 
ORDER BY shop_count DESC 
LIMIT 1;
Answer: The top business category in Thiruvananthapuram by number of shops.

22. Question: List the names of shops in the 'Services' category that have a verified default address and are currently active.

SQL Query:

SQL
SELECT s.name 
FROM shop_details s 
JOIN categories c ON s.category_id = c.id 
JOIN shop_address a ON s.id = a.shop_id 
WHERE c.categoriesname = 'Services' 
  AND s.status = 1 
  AND a.is_default = 1;
Answer: Names of active service-category shops where is_default = 1.

23. Question: What is the average discounted offer price for shops that have the word 'Juice' in their name?

SQL Query:

SQL
SELECT ROUND(AVG(o.offer_price), 2) AS avg_juice_offer 
FROM offer o 
JOIN shop_details s ON o.shop_id = s.id 
WHERE s.name LIKE '%Juice%';
Answer: The average offer_price across shops containing "Juice" in their name.

24. Question: Find the average review rating for products listed by shops in Kerala.

SQL Query: None (Unanswerable).

Answer: Refusal ("This question requires a verified relationship between shops and products, which is not currently configured").

25. Question: List the shop name, category name, and average review rating for all shops in the 'Fashions' category, ordered from highest to lowest rating.

SQL Query:

SQL
SELECT s.name, c.categoriesname, AVG(r.rating) AS avg_rating 
FROM shop_details s 
JOIN categories c ON s.category_id = c.id 
JOIN reviews r ON s.id = r.shop_id 
WHERE c.categoriesname = 'Fashions' 
GROUP BY s.id, s.name, c.id, c.categoriesname 
ORDER BY avg_rating DESC;
Answer: Ranked list of shops in the Fashions category based on their calculated average review rating.  
