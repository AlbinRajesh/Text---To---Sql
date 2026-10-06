# Project Overview: NoSQL AI Query Agent

## 1. What We're Building
An AI-powered chatbot that lets users ask questions in plain English and get accurate answers from our MongoDB (NoSQL) database — no manual query writing needed. Built to enterprise-grade standard: safe, self-correcting, fast, and traceable.

**Core challenge this solves:** MongoDB has no fixed structure (different records can have different fields), so a naive AI would constantly guess wrong. This system grounds every answer in real data and real business definitions instead of guessing.

## 2. Tech Stack

| Layer | Technology |
| :--- | :--- |
| **Frontend** | React (JSX) |
| **Backend** | FastAPI (Python) |
| **AI Orchestration** | LangGraph (StateGraph — graph-based, not a straight pipeline) |
| **Database** | MongoDB |

## 3. The Locked Architecture (v2) — 11 Nodes
Built as a graph, not a straight line — it can loop back and retry instead of breaking.

1.  **Query Classifier + Rewrite:** Cheap/fast model decides simple vs. complex, and cleans up vague questions (e.g., "active ones in asia" → "Show active projects in the APAC region").
2.  **Cache Check (upgraded):** Checks for a similar past question using pattern + detail matching (not raw similarity alone) — prevents mixing up "France" with "Germany" questions.
3.  **Schema-as-Data Grounding (SAG):** Pulls a few real documents from MongoDB so the AI knows actual field names/structure.
4.  **Grade the Sample:** Checks if the sample is good enough; if not, re-samples (capped at 3 retries).
5.  **Pull Business Rules:** Looks up our company's rulebook (JSON file) for what terms like "active" officially mean.
6.  **Generate the Query:** The strongest/most capable AI model writes the actual MongoDB query, grounded in real data + rules.
7.  **Safety Check:** Blocks dangerous operations before anything runs; database connection is read-only.
8.  **Run + Self-Fix:** Runs on a test copy; if it fails, sends the error back to AI to fix and retry (capped at 3 retries).
9.  **Result Grader (async):** User gets their answer immediately via a fast basic check; a deeper AI check runs after, in the background, flagging anything wrong without making the user wait.
10. **Return + Save:** Delivers the answer, saves the question + query to cache for next time.
11. **Human-in-the-Loop Escalation:** If any loop above fails too many times, the system stops and honestly says so instead of guessing — flags it for a human to review.

**Model usage rule:** Cheap/fast model for classification and grading steps (1, 4, 9) → strongest model reserved only for generation and repair (6, 8). Keeps cost under control without sacrificing accuracy where it matters most.

## 4. Build Phases

| Phase | What we build | Goal |
| :--- | :--- | :--- |
| **1** | Question-rewriter, business rulebook (JSON), real data sampling, query generation, safety filter, run query | Working, safe, correct basic flow |
| **2** | Retry-on-failure, re-sampling, stop-switch (escalation) | System can recover from its own mistakes |
| **3** | Smart cache (built correctly from day one), fast-reply + background double-check | Fast and cheap for repeated questions |
| **4** | Logging, access control (role/region-based), monitoring dashboard | Safe and traceable for real company use |
| **5** | Test question bank, LLM-as-Judge automated grading, human spot-checks, accuracy metrics tracked over time | Prove it actually works, catch regressions |
| **6 (future)** | Offline pre-computed schema snapshots, richer error diagnostics, self-hosted models at scale | Optimize further once usage justifies it |

## 5. Key Design Principles
*   **Never trust AI output blindly:** Every query passes a safety check before running, and the database connection itself is read-only.
*   **Ground everything in real data:** No guessing field names; always sample real documents first.
*   **Consistent definitions:** Business terms come from one shared rulebook, not the AI's own judgment each time.
*   **Fail honestly, not silently:** If the system can't confidently answer, it says so and escalates, rather than guessing.
*   **Don't block the user unnecessarily:** Deep verification happens in the background after the answer is delivered, not before.
*   **Prove it works:** A fixed test question bank + LLM-as-Judge + human spot-checks, re-run every time something changes.

## 6. Folder Structure

```text
project-root/
│
├── backend/
│   ├── app/
│   │   ├── main.py                      # FastAPI entrypoint
│   │   │
│   │   ├── api/
│   │   │   └── routes/
│   │   │       ├── chat.py              # POST /chat endpoint
│   │   │       └── health.py
│   │   │
│   │   ├── agent/                       # LangGraph orchestration
│   │   │   ├── graph.py                 # Defines the StateGraph + edges/loops
│   │   │   ├── state.py                 # Shared graph state schema
│   │   │   ├── nodes/
│   │   │   │   ├── classifier_rewrite.py    # Node 1
│   │   │   │   ├── cache_check.py           # Node 2
│   │   │   │   ├── sag_sampling.py          # Node 3
│   │   │   │   ├── grade_sample.py          # Node 4
│   │   │   │   ├── business_rules.py        # Node 5
│   │   │   │   ├── generate_query.py        # Node 6
│   │   │   │   ├── safety_check.py          # Node 7
│   │   │   │   ├── run_self_fix.py          # Node 8
│   │   │   │   ├── result_grader.py         # Node 9 (async)
│   │   │   │   ├── return_save.py           # Node 10
│   │   │   │   └── escalation.py            # Node 11
│   │   │   └── prompts/
│   │   │       ├── classifier_prompt.py
│   │   │       ├── generate_query_prompt.py
│   │   │       ├── grader_prompt.py
│   │   │       └── repair_prompt.py
│   │   │
│   │   ├── db/
│   │   │   ├── mongo_client.py          # MongoDB connection
│   │   │   └── cache_store.py           # Vector cache (embeddings + lookup)
│   │   │
│   │   ├── rules/
│   │   │   └── business_rules.json      # The company rulebook
│   │   │
│   │   ├── models/                      # Pydantic request/response schemas
│   │   │   └── chat.py
│   │   │
│   │   ├── services/
│   │   │   ├── embedding_service.py     # For cache matching
│   │   │   └── llm_service.py           # Model calls (cheap vs. strong model routing)
│   │   │
│   │   ├── utils/
│   │   │   ├── logging.py
│   │   │   └── audit.py                 # Audit trail logging
│   │   │
│   │   └── tests/
│   │       ├── question_bank.json       # Phase 5 — test question set
│   │       └── eval/
│   │           ├── llm_judge.py
│   │           └── run_eval.py
│   │
│   ├── requirements.txt
│   └── .env.example
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── ChatWindow.jsx
│   │   │   ├── MessageBubble.jsx
│   │   │   ├── QueryResultTable.jsx
│   │   │   └── Sidebar.jsx
│   │   ├── pages/
│   │   │   └── ChatPage.jsx
│   │   ├── api/
│   │   │   └── chatApi.js               # Calls the FastAPI backend
│   │   ├── hooks/
│   │   │   └── useChat.js
│   │   ├── App.jsx
│   │   └── main.jsx
│   ├── package.json
│   └── vite.config.js
│
├── docs/
│   ├── approach_note.md
│   └── architecture_diagram.png
│
└── README.md



#Aproch note-==============================

Approach Note: How We'll Build This (Simple Version)

*Phase 1 — Get the Basic Version Working*

Set up the question-rewriter — a small AI step that cleans up unclear questions before anything else happens.
Example: user types "active ones in asia" → AI rewrites it to "Show active projects in the APAC region"
Write the company rulebook (JSON file) — a simple file that defines what our business terms mean, so the AI doesn't guess.
Example:
Get real data samples from the database — pull a few real documents so the AI can see the actual field names/structure, instead of guessing.
Example: pulling 5 real project records so the AI knows fields are called status and endDate, not state or deadline
Set up the AI to write the query — using the rewritten question + rulebook + real samples, the AI writes the actual database query.
Add the safety filter — block any risky/destructive commands before a query is allowed to run.
Run the query — execute it and return the result.

Goal of this phase: a working question-to-answer flow, using real data and real company definitions — nothing fancy yet, just correct and safe.

*Phase 2 — Make It Self-Correcting*

Add retry-on-failure — if the query breaks, send the error back to the AI to fix it and try again (up to 3 times).
Add re-sampling — if the data sample wasn't good enough to answer the question, pull a better/more targeted sample and try again.
Add a stop switch — if it still can't get it right after a few tries, don't guess — say so honestly and flag it for someone to check.

Goal of this phase: the system can recover from its own mistakes instead of just breaking.

*Phase 3 — Make It Fast*

Add the smart cache — save previously answered questions so similar future questions get instant answers.
Important: built correctly from day one — the cache checks the actual details (like region/date), not just "sounds similar," so it never mixes up different questions (e.g., won't confuse "France" with "Germany").

*Phase 4 — Make It Enterprise-Ready*
Log everything — every question, every query, every answer, saved for review.
Restrict access — users only see the data they're allowed to see.
Add a dashboard — track how often the cache is used, how often retries happen, and how often something gets flagged.

Goal of this phase: safe, traceable, ready for real company use.

*Phase 5 — Evaluate the System (Before Calling It Done)*

Before trusting this in real use, we test it properly — not just "it seems to work."

Build a test question bank — a set of real, varied sample questions with known-correct answers (easy ones, tricky ones, edge cases). This becomes our repeatable test set every time we change something.
LLM-as-Judge — use a separate AI to automatically grade each answer: did it actually answer the question correctly, using real data? This lets us test hundreds of questions quickly, without a human checking every single one.
Human manual evaluation — a person manually reviews a smaller sample of results (especially tricky/important ones) to confirm the AI judge is actually grading correctly, and to catch anything an AI judge might miss.
Track key accuracy metrics — not just "right or wrong," but specifically:
Did it use the right data fields? (didn't miss anything needed)
Was the answer actually grounded in real data? (not made up)
Did the query even run successfully? (technical success rate)
Re-run this test set regularly — every time we change a prompt, model, or step in the pipeline, run the same question bank again to make sure nothing got worse (this catches "we fixed one thing but broke another").
Goal of this phase: faster and cheaper for repeated questions, without slowing down what the user actually experiences.

-----------------------------------------------------------------------------------------
If your current viewer does not support Mermaid, here is the exact structural flow of your agent in plain text:

User Input: Receives the natural language question.

Node 1 (Classifier): A fast LLM rewrites and extracts entities from the query.

Node 2 (Cache): Checks if this query was answered before.

Hit: Skips straight to Node 10 (Return).

Miss: Moves to Node 4.

Node 4 (Grade Sample) & Node 3 (Schema): Node 4 checks if it has enough context. If not, it pulls real table structures from Node 3.

Node 5 (Business Rules): Attaches your custom definitions (like "premium tier") to the prompt.

Node 6 (Generate Query): A heavy, powerful LLM takes all the context and writes the SQL dictionary.

Node 7 (Safety Check): A strict Python script checks the SQL for malicious injections.

Failed: Kicked to a human reviewer (Node 11).

Passed: Moves to execution.

Node 8 & MySQL (Execution): Runs the query against the database. If MySQL throws a syntax error, Node 8 catches it and asks the LLM to fix it automatically.

Node 9 (Grader): A fast LLM checks the final result to make sure it actually answers the user's question.

Node 10 (Return): The final data is sent back to the user, and the successful query is cached for next time.