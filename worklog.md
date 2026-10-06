Project Handoff Summary — NoSQL→SQL AI Query Agent
1. Project Overview

Building an 11-node LangGraph pipeline that converts natural language questions into safe, read-only SQL queries against MySQL. Originally designed for MongoDB/NoSQL, migrated to MySQL. Backend: FastAPI + Python. Frontend: React (not started). LLM providers: Groq (primary) + Gemini (added for specific roles).

2. Current State & Where We Left Off

Just finished testing Node 4 (grade_sample.py) — it passed all tests. This was the last untested-but-built node. We were about to start state.py (shared graph state schema) as the next step, since graph.py can't be wired without it.

3. Locked Node Status
Node	File	Status
3 — Schema Grounding	schema_grounding.py	✅ Built + tested. Caches schema from INFORMATION_SCHEMA on startup, refresh_schema_cache(), get_cached_schema().
4 — Grade Sample	grade_sample.py	✅ Built + tested. Uses Gemini (SCHEMA_GRADER role) to judge if cached schema suffices; refreshes + retries up to 3x; raises GradeSampleError → routes to Node 11.
6 — Generate Query	generate_query.py	✅ Built + tested. Uses Groq strong model (QUERY_GENERATOR role). Fixed _valid_columns_for_table() to read new schema shape {table: [{"column":..., "data_type":...}]}.
7 — Safety Check	safety_check.py	✅ Built + tested (rewritten with AST). Two-layer defense: (1) builds parameterized SQL from trusted dict, (2) re-parses with sqlglot to structurally verify SELECT-only, no forbidden statement types, table/column cross-check against real schema. Signature changed: run_safety_check(query, valid_tables, schema_context) — third arg now required. Known quirk: must replace %s placeholders with "0" before sqlglot.parse() (sqlglot doesn't understand %s, treats it as modulo) — real SQL with real %s is still returned/executed untouched.
8 — Run + Self-Fix	run_self_fix.py	✅ Rewritten from MongoDB to MySQL (Phase 1 only — no self-fix retry loop yet, that's Phase 2). Uses fetch_all_readonly() against the read replica.
1, 2, 5, 9, 10, 11	classifier_rewrite.py, cache_check.py, business_rules.py, result_grader.py, return_save.py, escalation.py	❌ Not built (empty files)
Orchestration	graph.py, state.py	❌ Not built (empty files) — nothing wires the nodes together yet
Frontend	All React files	❌ Not built — only Vite starter page exists
4. Infrastructure Locked-In

mysql_client.py — ✅ Done. Two pools: pool (primary, admin/schema reads only) and read_pool (replica, least-privilege user, session timeout MAX_EXECUTION_TIME=8000ms, wait_timeout=30). Functions: connect_to_mysql(), connect_to_read_replica(), fetch_all() (primary), fetch_all_readonly() (replica).

main.py lifespan() — ✅ Updated to call, in order: connect_to_mysql() → connect_to_read_replica() → refresh_schema_cache() on startup.

llm_service.py — ✅ Just extended to support two providers: Groq and Gemini, routed per-role via .env. Key additions: SUPPORTED_PROVIDERS = {"groq", "gemini"}, _call_groq() and _call_gemini() split out, _client_for() branches on provider (AsyncGroq vs genai.Client). Requires pip install google-genai. Gemini calls flatten messages into one prompt string (no multi-turn roles) and use client.aio.models.generate_content().

Current .env role assignments:

CLASSIFIER → Groq, openai/gpt-oss-20b
SCHEMA_GRADER → Gemini, gemini-2.0-flash-lite (fixed from invalid gemini-3.5-flash-lite)
QUERY_GENERATOR → Groq, openai/gpt-oss-120b
QUERY_REPAIR → Groq, openai/gpt-oss-120b
RESULT_GRADER → Gemini, gemini-2.0-flash-lite

⚠️ Security flag not yet resolved: .env still has MYSQL_USER=root for the primary pool — least-privilege only exists on the read replica side. Also, real API keys were pasted in plain chat during this session — rotate the Groq and Gemini keys before going further.

5. Test Files Status
Test	Status
test_schema_grounding.py	✅ Passing (replaces old test_sag_sampling.py, which should be deleted)
test_safety_check.py	✅ Passing (8/8, after %s→"0" fix)
test_generate_query.py	✅ Passing
test_grade_sample.py	✅ Passing
test_mysql_client.py, test_run_self_fix.py	Untouched, presumed still fine
test_business_rules.py, test_classifier_rewrite.py	Stub tests for unbuilt nodes
6. Immediate Next Steps (in order)
Build state.py — define the shared state schema that flows through all 11 nodes (question, rewritten_question, cache_hit, schema_context, matched_rules, generated_query, safety_result, execution_result, retry_counts, etc.)
Build graph.py — wire nodes 1–11 together using LangGraph's StateGraph, including the retry loops (Node 4↔3, Node 8 self-fix loop, Node 9 async) and escalation routing to Node 11 on cap-out.
Build remaining empty nodes: 1 (classifier_rewrite), 2 (cache_check), 5 (business_rules), 9 (result_grader), 10 (return_save), 11 (escalation).
Build FastAPI chat.py route to expose the graph via /api/chat.
Build frontend (chatApi.js, useChat.js, ChatWindow.jsx, etc.) to consume that route.
Fix .env: rotate exposed keys, remove MYSQL_USER=root for primary pool (use least-privilege there too, or restrict primary pool's use to schema-only reads as currently intended).

User preference note for next session: keep explanations short/plain, but give full detailed code for anything coding/debugging related.
=========================================================================================
Project Handoff: NoSQL→SQL AI Query Agent (LangGraph, MySQL)
1. Project Overview

11-node LangGraph pipeline converting natural-language questions into safe, read-only SQL on MySQL. Stack: FastAPI + Python backend, React frontend (not started). LLMs: Groq + Gemini, routed per role via .env. Embeddings are now local (fastembed).

User preferences: short, simple explanations; no jargon; full code always; enterprise-grade fixes; diagnose before prescribing; explain bugs as (a) what went wrong (b) why (c) fix.

2. Current State & Where We Left Off

Active issue: Gemini free-tier quota (15 requests/min per model).

SCHEMA_GRADER (Node 4), QUERY_GENERATOR (Node 6, just moved from Groq to gemini-3.5-flash-lite), and RESULT_GRADER (Node 9) all use gemini-3.5-flash-lite, so they share one 15/min limit. A question makes about 3 Gemini calls, and the 12-question test made about 30 calls in about 50s.
Symptom: "What is the average price of all plans" was escalated with Last known missing items: []. It was a 429, not a schema problem.
Two bugs found, not yet fixed:
grade_sample.py _ask_cheap_model_if_sufficient catches every exception and returns (False, []), so quota/API errors look like "schema insufficient" and reach users as "could not be answered safely".
llm_service.py retries after 1s and 2s, but Gemini says "retry in ~50s", so retries never help and waste quota.
Options presented (user has not chosen): (1) move Node 6 back to Groq to split load across two providers, (2) paid tier, (3) let LLMServiceError propagate from Node 4 as an infrastructure error. Also offered: a rate limiter that waits instead of failing.

Files being modified: .env (role provider/model), llm_service.py, grade_sample.py (pending), graph.py, test_graph.py.

Other open observations:

Node 6 on the lite model returned SUM(price) grouped by name for "price of each plan as total_price". Node 9 passed it. The numbers happened to match, but it is the wrong query. Watch for regressions vs. gpt-oss-120b.
Groq gpt-oss-120b had 429 waits of 12–34s in Node 6 (probably a tokens-per-minute limit; not confirmed). Idea: compact the schema JSON in generate_query_prompt.py (it uses indent=2).
Off-topic questions ("meaning of life", Bitcoin) exhaust 3 Node 4 rounds and go to the human-review queue. Design decision pending: return "can't answer from this database" instead.
Classifier rewrite can alter the question ("price of each plan as total_price" became "total price of each plan").
Cache check runs after the Groq classifier call, so a hit still costs one Groq call (0.6s).
3. Code & Fixes Locked-In

Node status (all built and working):

Node	File	Notes
1	classifier_rewrite.py	Groq gpt-oss-20b
2	cache_check.py	In-memory semantic cache, threshold 0.90, plus a guard: cache hits require identical numbers in both questions (_numbers_in)
3+4	schema_grounding.py, grade_sample.py	Hybrid retrieval, top_k = 6, 12, 18 across 3 attempts
5	business_rules.py	
6	generate_query.py	Reasoning-first prompt, aggregate mapping block, MAX_ATTEMPTS=2 retry on validation failure only
7	safety_check.py	Two layers (builder plus sqlglot AST). 13/13 direct unit tests pass in test_safety_check.py
8	run_self_fix.py	Phase 1 only, no self-fix loop
9	result_grader.py	Blocking LLM grade; invalid means escalate
10	return_save.py	Caches only unanswerable verdicts or Node-9-valid results
11	escalation.py	Writes to logs/escalations.jsonl

Fixes confirmed:

node_execute in graph.py: expands * only when aggregate is None. It uses alias if present, else agg_column labels. The leftover else: bug was deleted.
generate_query_prompt.py: reasoning is the first JSON field, and there is a CRITICAL line plus a "how many → COUNT(*)" mapping block. This fixed the missing-reasoning and COUNT failures.
embedding_service.py: swapped Gemini embeddings for local BAAI/bge-small-en-v1.5 via fastembed (cache_dir="models_cache", in .gitignore). .env must NOT set SCHEMA_EMBEDDING_MODEL=gemini-.... The Gemini embedding quota problem is gone.
llm_service.py: Gemini client timeout plus asyncio.wait_for (30s + 5s), asyncio.TimeoutError retried, and per-call timing via record_llm_call.
timing.py (app/services/timing.py): per-node timing via a ContextVar, with timed_node, start_request, record_llm_call, format_timing_report. graph.py wraps all nodes with timed_node and run_pipeline attaches final_state["timings"].
state.py: EscalationInfo.node Literal widened to include classifier_rewrite, generate_query, safety_check.
test_graph.py: 12 questions (including injection and cache-repeat), timestamped logging, per-node timing report.

Pending or unverified:

Tokenizer fix in embedding_service.py: replace _tokenize with a regex ([a-z0-9]+) and fix the guaranteed block to use token-subset matching. Given earlier; not confirmed applied. "Show me all plans." once retrieved 6 tables without plans.
test_graph.py: add await asyncio.sleep(5) between questions to stay under the Gemini quota when testing.
Injection tests never reach safety_check through the full pipeline (Groq refuses, or Node 1 rewrites to "show all plans"). The unit tests are the real proof.
Score on the latest run: 11/12 correct (the failure was quota-caused).
4. Immediate Next Steps
Decide the quota fix. Recommended: move QUERY_GENERATOR back to Groq (openai/gpt-oss-120b, with its own Groq key) to split the load. Set all three .env lines together: provider, model, key.
Fix Node 4 error handling (grade_sample.py): let LLMServiceError propagate instead of returning (False, []), and route it in graph.py as an infrastructure error (not a schema-insufficient escalation).
Fix retry backoff for 429s in llm_service.py (honor the provider's "retry in Ns" or add a rate limiter).
Apply the tokenizer fix, add a sleep(5) between test questions, and re-run test_graph.py.
Then build: FastAPI chat.py route (/api/chat calling run_pipeline), then the React frontend (chatApi.js, useChat.js, ChatWindow.jsx).
Later: Node 8 self-fix loop (Phase 2), Node 9 to Node 6 retry with grader feedback, a decision on off-topic escalation, and Groq/Gemini paid-tier capacity planning.



Your list started at item 2, so I added a short project overview at the top as item 1. Everything is in one block. Paste it as the first message in the new chat.

# HANDOFF: "Ask Your Data" NL-to-SQL agent (continue from here)

## 1. Project and working rules
- Project: 11-node LangGraph pipeline turning questions into safe read-only SQL.
  FastAPI + Python, MySQL, Groq + Gemini LLMs. Repo: D:\ngxp_internship\nosql_agent\backend
- I use Windows PowerShell with venv. Test command:
  .\venv\Scripts\python.exe -m pytest app/tests -q
- KNOWN, EXPECTED failures (2): test_average_rating_contract_builds_the_required_count_query
  and test_average_rating_contract_lists_each_qualifying_shop (in test_join_queries.py).
  Anything else failing is new.
- I apply code edits with GitHub Copilot in VS Code. Its terminal sandbox is denied,
  so prompts to Copilot must say "do NOT use the terminal, edit files only"; I run
  the tests myself. Give me prompts/edits in exact, copy-paste form.
- Explain non-code things in short, simple English. Give complete code when coding.
- ASK ME BEFORE any run that calls external LLMs (benchmark). Always use a NEW
  --output file; never overwrite advanced_query_generation_benchmark.csv.
  My baseline copy: app\tests\baseline_2026-10-03_before_fixes.csv.
- LangGraph gotcha: GraphState is a TypedDict, so only use existing state keys.

## 2. Current state and exactly where we stopped
Plan phases (original plan + 10 review amendments):
- Phase 1 DONE: llm_service.py. Groq max_retries=0; call_agent is the only retry
  owner; waits at least retry-after (retry-after-ms preferred); if the wait is over
  LLM_MAX_RETRY_AFTER_SECONDS (60) it raises LLMServiceError instead of retrying
  early; no header -> floor LLM_RATE_LIMIT_FLOOR_SECONDS (10); redacted 429 header
  logging. Tests: app/tests/test_llm_service_retries.py (22 pass).
- Phase 0 DONE: Node 6 rejection diagnostics in generate_query.py. Structure-only
  logs (no filter values) for numeric aggregates (both predicates
  aggregate_on_base_table / all_joins_many_to_one) and for joined-COUNT rejections
  via _log_rejected_count_plan. Tests: test_node6_rejection_logging.py.
- Phase 2 DONE: stable rule_id + stage on rejections.
  QueryGenerationError / SafetyViolationError now carry .rule_id and .stage.
  rule_ids: joined_count_not_distinct, joined_count_not_primary_key,
  fanout_select_aggregate (generator + safety build), fanout_having_aggregate
  (safety build), fanout_ast_aggregate (safety AST). Advanced benchmark now has
  outcome (answered / confirmed_refusal / escalated / service_failure /
  validation_rejected), result (pass/fail), rule_id, error_stage, llm_calls_total,
  llm_calls_by_role, llm_attempts_by_provider. Case 9 expects
  fanout_select_aggregate at generator; case 16 expects fanout_having_aggregate at
  safety_check. Fixtures: round_digits removed from cases 6 and 23; case 7 note
  rewritten. Case 10 intentionally unchanged (waits for Phase 6). Tests:
  test_rule_ids.py, test_benchmark_scoring.py.
- Phase 3 DONE: typed refusal. grade_sample.py has MissingRelationshipError (NOT a
  GradeSampleError). Grader prompt now asks for typed missing items
  {type: table|column|relationship, detail, tables}; plain strings still accepted.
  Early refusal ONLY when a "relationship" item names exactly two distinct real
  tables with NO path (any hops) in the FULL catalog. Missing columns, unknown
  types, outages still widen/escalate. graph.py: node_grade_schema sets
  answerable=False + unanswerable_reasoning (no table names in user text);
  route_after_grade_schema -> "return_save" for that case. Benchmark counts only
  UnanswerableQuestionError / MissingRelationshipError as confirmed_refusal.
  Tests: test_grade_sample_typed_refusal.py (13 pass). Full suite: 124 pass + the 2 known.

EXACTLY WHERE WE STOPPED:
- I was about to run the first LIVE benchmark (approved by me): 
  .\venv\Scripts\python.exe -m app.tests.advanced_query_generation_benchmark --cases 4,9,13,16,21 --output app\tests\phase3_check.csv
- It crashes BEFORE connecting to anything (no LLM calls made):
  NameError: name '_parse_case_numbers' is not defined, at
  app\tests\advanced_query_generation_benchmark.py line 534, in main().
  The file imports the helper as _parse_case_numbers_impl (pre-existing bug, only
  triggers with --cases). The fix has NOT yet been applied on disk (same traceback
  3 times). I was asked to run:
  Select-String -Path app\tests\advanced_query_generation_benchmark.py -Pattern "_parse_case_numbers"
  and to apply the fix.

## 3. Code and fixes
PENDING FIX (apply first): in advanced_query_generation_benchmark.py main(), line 534:
    selected = _parse_case_numbers_impl(args.cases) if args.cases else None
If editing by hand keeps failing, this PowerShell command applies it:
  $p = "app\tests\advanced_query_generation_benchmark.py"
  $t = [System.IO.File]::ReadAllText((Resolve-Path $p))
  $t = $t.Replace("_parse_case_numbers(args.cases)", "_parse_case_numbers_impl(args.cases)")
  [System.IO.File]::WriteAllText((Resolve-Path $p), $t, (New-Object System.Text.UTF8Encoding($false)))
  Select-String -Path $p -Pattern "_parse_case_numbers"

THEN run the 5-case benchmark above and ask me to paste: console output plus these
CSV columns per case: case_number, outcome, result, rule_id, error_stage,
llm_calls_total, error (plus any 429 log line). What to expect:
- Cases 4, 21: confirmed_refusal (grader names shop_address + shop_details, no path).
- Case 13: probably still escalated (grader says missing column bookings.user_id;
  real column is requester_id; Phase 4 fixes the hint).
- Case 9: pass, fanout_select_aggregate at generator.
- Case 16: pass, fanout_having_aggregate at safety_check.

Files changed so far: generate_query.py, safety_check.py, grade_sample.py, graph.py,
llm_service.py, query_generation_benchmark.py, advanced_query_generation_benchmark.py
(untracked/new), test_standalone.py (typo fix on line 19: logging), plus new tests
listed above. business_rules.json was edited by the DB owner (discount definition).

Old-baseline findings (CSV before fixes): cases 4,13,20,21,22,24 ended as generic
GradeSampleError (Phase 3 targets these); cases 11,15,25 fail the aggregate guard;
case 16 was rejected at the safety check, not the generator; case 5 was marked
refused although the model's own reasoning said it could answer; cases 6,10,23 are
valid SQL but fail on fixtures (round_digits, name-vs-ID).

## 4. Remaining plan (in order)
PHASE 4 (needs the relationship catalog file; not yet located. Find it with:
  Get-ChildItem -Recurse -Include *.py,*.json -Exclude venv | Select-String -Pattern "reviewed_bookings_shop_id" -List | Select-Object Path )
- DB owner CONFIRMED and cleaned: shop_address.shop_id -> shop_details.id and
  bookings.requester_id -> users.id (both many_to_one; add to catalog). Cleanup done:
  27 orphan addresses deleted; shop 210 now has only its newest address is_default=1.
- DO NOT add: products->shop (owner: products.user_id matches shop_details.partner_id
  in 256 records but suggests routing through users; intended link NOT confirmed),
  subscriptions->users, "verified address" (no column).
- Location rule (policy to confirm with me; suggested is_default = 1 only): a location
  filter makes it an inner join, so shops without an address are excluded by design;
  LEFT JOIN only for questions with no location filter; use COUNT(DISTINCT
  shop_details.id). Before choosing, run: shops with no address row; shops where every
  is_default is NULL or 0 (count NULL separately).
- Fix grader hint for case 13 (requester_id, not user_id).
- Decide each case's expected outcome one at a time AFTER the joins exist (case 5 may
  still be refused by the fan-out guard). Cases 17, 22, 24 stay refusals unless the
  owner confirms otherwise.
- Discount = actual_price - offer_price (owner's definition). The plan format has no
  arithmetic: check if it can express this safely; if not, cases 2 and 7 stay
  refusals ("no safe calculation"). Case 7 also needs a date policy and an
  offer->category path (offer.category_id is not a trusted relationship).
PHASE 5 (gate: Phase 0 per-predicate evidence; never loosen on log evidence alone):
  first try prompt guidance (put the measured table first as base for SUM/AVG). Case 15
  (COUNT(DISTINCT reviews.user_id)) violates the primary-key count rule: ask me whether
  to allow a verified FK column or join users and count users.id. Cases 9 and 16 MUST
  stay blocked; add tests proving fan-out cases are still rejected.
PHASE 6 (gate: my decision): remove hard-coded subcategory 9 / category 2 from
  classifier_prompt.py; preferred = filter by name via the verified join; subcategory
  names repeat ("All", "More") so ambiguity must be handled; align fixtures for cases
  6 and 10.
PHASE 7: rerun full benchmark (ask me first), compare against the baseline CSV case by
  case and explain every difference, run all tests, report remaining failures honestly.
  The benchmark never executes SQL, so hand-check 5 cases against the DB (ask me to
  approve the case list first; no personal data in output).

OPEN DECISIONS (ask me, do not assume): 1) location rule, 2) names vs IDs,
3) whether to loosen the aggregate guard (only after Phase 0/5 evidence).

Start the new chat with that block, then add: "First, help me apply the pending fix and run the 5-case benchmark."