
---improvemtn---------------------------------------====
No cap on cost/token usage per request for the "strongest model" nodes — at scale this is a real budget risk.
No mention of rate limiting per user (someone could spam expensive query-generation calls).
"Dialect transpilation" (point 1) implies multi-DB support — but you're only targeting MySQL right now. That's premature scope; note it but don't build it yet.
-----------------------------------------------------------------------------------------
The problem: your pipeline asks Gemini too many times per minute, and Gemini's free tier only allows 15 per minute.

Example: one question makes 3 Gemini calls: Node 4 (check schema), Node 6 (write query), Node 9 (check result). Your test sent 12 questions in about 50 seconds. That is roughly 30 calls, double the limit.

Question 8, "What is the average price of all plans", hit the wall:

Node 4 asks Gemini "is the schema enough?"
Gemini replies "429, you're over your limit, wait 52 seconds"
Your code retries after 1s, then 2s. Both fail.
Node 4 treats the failure as "schema is not sufficient" and escalates.
The user sees "could not be answered safely", even though the question was fine.

Why it happened now: before, Node 6 used Groq, so Gemini only got 2 calls per question. Moving Node 6 to Gemini made it 3, and all three share the same 15-per-minute limit.

Two bugs inside this:

Your retries wait 1-2s, but Gemini says wait about 50s, so they never help.
Node 4 reports a quota failure as "schema not sufficient", which is a wrong reason.

Fix options:

Move Node 6 back to Groq. That splits the load across two providers.
Pay for a higher tier.
Make Node 4 report quota errors as errors, not as "insufficient schema".

Tell me which one you want and I'll write it.
----------------------------------------------------------------------------------------
What the problem is

Your app connects to the database as root. Root is the all-powerful admin account: it can read, change, and delete anything, including whole tables.

What we verified

Your .env sets MYSQL_USER=root and MYSQL_READ_ONLY_USER=root. The second one is named "read-only", but it is also root, so nothing is read-only.
SHOW GRANTS confirmed root has every permission, including DROP (delete tables) and FILE (read files on the server).
The tables also hold private data: users, otps (login codes), oauth_access_tokens, password_reset_tokens. The AI can currently reach all of them.

What protects you today

safety_check.py only allows a single SELECT and checks tables and columns against the schema. That is a real protection, and it blocks the obvious attacks.

Why it still matters

The safety check is code, and code can have gaps. An AI-generated query is untrusted input. If one bad query gets past the check, root permissions turn a small mistake into a disaster. A read-only user limits the damage to "someone read data" instead of "someone deleted the database".

The bigger everyday risk is privacy. A user could ask "show me all phone numbers from the users table", and if the safety check allows that table, the AI answers. That is a data leak, not a hack, and it is easy to trigger by accident.

Is it urgent?

Local testing with fake or test data: low urgency. Do it before you show this to anyone else.
Real users or real customer data: high urgency. It should be fixed before launch. Enterprise security reviews will flag root immediately.

Effort: about 15 minutes. Create one user with SELECT on a few tables and change two lines in .env.

My suggestion is to keep working on answer quality now and put this on your pre-launch checklist. It does not block anything else we are doing.




--------------------------------------------------------------------------------------------
The problem

What went wrong: You typed 'Lucky Users Special Offer' (no !) and got "No matching records found," even though the offer exists.

Why: The SQL compares text with =, and = needs every character to match. The database value is Lucky Users Special Offer!, so one missing character gives 0 rows. The grader then called the empty result VALID, and the cache stored it.

Fix: When a text filter returns 0 rows, look up similar values and ask the user to confirm.

SQL generated today (from your log)
sql
SELECT /*+ MAX_EXECUTION_TIME(10000) */
    `shop_details`.`phone` AS `shop_phone`,
    `shop_address`.`arearoadname` AS `area_road_name`
FROM `offer`
INNER JOIN `shop_details` ON `offer`.`shop_id` = `shop_details`.`id`
INNER JOIN `shop_address` ON `shop_address`.`shop_id` = `shop_details`.`id`
WHERE (`offer`.`offer_heading` = %s) AND (`shop_address`.`is_default` = %s)
LIMIT %s
-- PARAMS: ('Lucky Users Special Offer', 1, 201)  -> 0 rows

This SQL is correct. It did exactly what it was told, and the input was the problem. So do not fix this in the SQL generator.

SQL the "did you mean" step should run

It runs only after the main query returns 0 rows. It is a separate, small, read-only query on one table:

sql
SELECT /*+ MAX_EXECUTION_TIME(5000) */ DISTINCT `offer`.`offer_heading`
FROM `offer`
WHERE `offer`.`offer_heading` LIKE %s
LIMIT 5
-- PARAMS: ('Lucky Users Special Offer%',)

It returns Lucky Users Special Offer!, and the reply is:

No offer named "Lucky Users Special Offer" was found. Did you mean "Lucky Users Special Offer!"?

Why not silently switch = to LIKE
LIKE '%Lucky%' could match several offers from different shops, and you would show the wrong shop's phone number as if it were certain.
The user never confirmed the guess.
For an enterprise app, a wrong answer shown with confidence is worse than asking.
Rules the step must follow (edge cases)
Run only when rows = 0 and the plan has a text = filter. Never on number filters.
Use a prefix match ('text%') first. It can use a database index. '%text%' cannot, so it is slow on big tables.
Escape % and _ in the user's text. These are wildcards in LIKE. Without escaping, a user typing % would match every offer.
Always pass the text as a parameter (%s), never build it into the SQL string. This blocks SQL injection (a user sneaking SQL commands into the text).
Show at most 5 suggestions. If there are none, say "No matching records found."
Do not cache the 0-row answer or the suggestion.
Apply the same access rules as the main query. Suggestions must not reveal values the user can't normally see.
Test cases
#	Input	Expected
A	...'Lucky Users Special Offer!'	1 row: phone 8807057825
B	...'Lucky Users Special Offer' (no !)	0 rows, then suggestion Lucky Users Special Offer!
C	...'Zzzz Nonexistent Offer'	0 rows, no suggestion, "No matching records found."
D	Offer name = %	Escaped, so it matches nothing and no suggestions come back
E	Run B twice	Both times the suggestion appears, because nothing was cached

Today: A passes. B, C and E fail. D is untested, and it must pass before you ship, because it is a security check.

To write the code, paste generate_query.py (where the filters are built), run_self_fix.py (where the query runs) and return_save.py (where caching happens). I haven't seen them, so I won't guess function names.

--------------------------------------------------------------------------------------------
Both fail because the query builder can't do math between columns. That is one fix, not two. It touches the safety code, so it needs tests and a team decision. Earlier you were also deciding what "best deals" means, so decide these together    

--------------------------------------------------------------------------------------------
Here is the node-by-node testing plan specifically designed for your Coupown multi-agent architecture.

This plan isolates each component to ensure that if the pipeline fails, you know exactly which agent dropped the ball.

1. Classifier Node (Intent & Rewrite)
This node’s job is to route the query and normalize the vocabulary.

Intent Routing Accuracy (Binary Test): Feed a dataset of 50 conversational prompts ("Hi", "Who made you?") and 50 data prompts. Assert that 100% of conversational prompts map to chat and data prompts map to data.

Rewrite Fidelity (Regex/String Test): Feed questions containing company vocabulary (e.g., "restaurants", "food places"). Assert that the rewritten output explicitly contains the exact strings required by the prompt (e.g., subcategory named 'Restaurent') and strictly contains no numeric IDs.

Rate-Limit Mocking (Resilience Test): Intentionally trigger a mocked 429 Too Many Requests error from the Groq API. Verify that the agent correctly applies the exponential backoff delay instead of crashing the pipeline.

2. Schema Grader Node (Retrieval & Filtering)
This node’s job is to fetch the right tables and pass them forward without crashing.

Precision & Recall Scoring: For 50 test questions, define the exact "Golden Tables" needed.

Recall Check: Did it grab all necessary tables? (e.g., shop_details and shop_address).

Precision Check: Did it grab fewer than 12 tables total? (Penalize the test if it blindly grabs 20 tables, which wastes tokens).

The "Managerial Refusal" Test: Pass a query asking for a non-existent column (like the "verified address" from Benchmark Case 22). Assert that the node exhausts its 3 attempts, successfully catches the error, and passes the remaining schema to the Query Generator without escalating to a crash.

3. Query Generator Node (Logic & SQL Writing)
This is the "brain" of the pipeline, using Gemini to write the SQL.

Execution Equivalence (Dataframe Comparison): Execute the generated SQL and your pre-written Golden SQL against a local test database. Assert that df_generated.equals(df_golden) is True. This ignores formatting and checks if the AI actually got the right answer.

Business Rule Adherence (Logic Test): Feed a question containing the word "discount".

Pass condition: The SQL contains (actual_price - offer_price).

Refusal condition: If the prompt restricts access to the offer table, assert that the node throws an UnanswerableQuestionError (Testing your exact fix for Case 2).

Hallucination Check (Faithfulness): Parse the generated SQL and assert that every single column and table used exists in the schema dictionary provided by the Schema Grader.

4. Safety Checker Node (AST Validator)
This node acts as your firewall before anything hits the MySQL database.

Adversarial Injection (Red Teaming): Feed the node malicious SQL strings (DROP TABLE users;, DELETE FROM shop_address;, 1=1; TRUNCATE). Assert a 100% block rate with a SafetyViolationError.

Invalid Aggregation Check: Feed the node a query that performs a numeric aggregate (SUM) on a child table across a one-to-many join (like Benchmark Case 3). Assert that the AST validator correctly flags it and rejects the plan.

Strict WHERE/LIMIT Enforcement: Assert that the validator successfully injects LIMIT X and confirms the presence of required join conditions on every generated query.

5. Full Pipeline (End-to-End Integration)
Hop-Count & Latency Profiling: Run the full 25-case benchmark. Track the execution time of each node. Fail the test if the end-to-end processing (excluding API rate-limit delays) exceeds 10 seconds per query, ensuring production readiness.