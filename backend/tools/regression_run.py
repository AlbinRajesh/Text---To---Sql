"""
regression_run.py (Step 0 safety net)

Runs a fixed list of questions through the REAL pipeline (run_pipeline),
saves what happened to a JSON file, and can compare two saved runs.

Where to put it:  backend/tools/regression_run.py
                  (also create an empty backend/tools/__init__.py)

Run from the backend folder, with the venv active:
    python -m tools.regression_run --label baseline
    python -m tools.regression_run --label step1 --compare baseline

Notes:
  * It uses your real database and your real LLM keys, so it costs tokens
    and counts toward rate limits. Use --delay to space the calls out.
  * Every run is a fresh process, so the in-memory answer cache starts
    empty. Results can never be served from a stale cache.
  * Result files contain real shop data (phone numbers). Keep them out of
    git: add  backend/tools/results/  to .gitignore.
  * The startup steps below copy lifespan() in app/main.py. If you change
    the startup code there, change it here too.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agent.graph import run_pipeline
from app.agent.nodes.business_rules import get_business_rules_store
from app.agent.nodes.schema_grounding import get_cached_schema, refresh_schema_cache
from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
)
from app.services.embedding_service import build_schema_index

# ---------------------------------------------------------------------------
# EDIT THIS LIST: put your own 8 questions here, worded exactly as you type
# them in the app. Add more any time; keep the old ones so you can compare.
# ---------------------------------------------------------------------------
QUESTIONS: List[str] = [
    "looking for a good restaurant near Technopark",
    "any cheap juice places in Nagercoil?",
    "any shops with a website i can check out",
    "is there anyone who cuts trees",
    "best rated place in Kazhakkoottam",
    "best rated place in Nagercoil",
    "need a car mechanic in Thiruvananthapuram with good reviews",
    "kingstar juice phone number pls",
    "show me the best deals right now",
]

# Row counts you have verified yourself with reference SQL. Add more as you
# confirm them. A mismatch is flagged in the report.
EXPECTED_ROW_COUNTS: Dict[str, int] = {
    "any shops with a website i can check out": 15,
}

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# A change in any of these fields between two runs is shown in the report.
COMPARE_FIELDS = (
    "intent", "rewritten_question", "answerable", "escalated", "escalation_reason",
    "answer_type", "row_count", "sql", "params",
    "schema_ok", "schema_tables", "unanswerable_reasoning",
)
# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

async def startup() -> None:
    """Same steps as lifespan() in app/main.py, minus the Redis suggestion store."""
    await connect_to_mysql()
    await connect_to_read_replica()
    await refresh_schema_cache()
    await build_schema_index(get_cached_schema())
    get_business_rules_store().version  # loads the business rules file; raises if broken


def summarise(state: Dict[str, Any]) -> Dict[str, Any]:
    """The facts we compare between runs. .get() everywhere: not every field exists on every path."""
    classifier = state.get("classifier_result") or {}
    safety = state.get("safety_check_result") or {}
    execution = state.get("execution_result") or {}
    final = state.get("final_answer") or {}
    escalation = state.get("escalation_info") or {}
    return {
        "intent": classifier.get("intent"),
        "rewritten_question": classifier.get("rewritten_question"),
        "answerable": state.get("answerable"),
        "escalated": bool(state.get("escalated")),
        "escalation_reason": escalation.get("reason"),
        "sql": safety.get("sql"),
        "params": list(safety.get("params") or []),
        "row_count": execution.get("row_count"),
        "answer_type": final.get("answer_type") if isinstance(final, dict) else None,
        "final_answer": final,
    }


async def run_all(label: str, delay: float, questions: List[str]) -> Dict[str, Any]:
    results = []
    for number, question in enumerate(questions, start=1):
        started = time.perf_counter()
        entry: Dict[str, Any] = {"question": question}
        try:
            state = await run_pipeline(question, request_id=str(uuid.uuid4()))
            entry["summary"] = summarise(state)
        except Exception as exc:  # one bad question must not stop the whole run
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["seconds"] = round(time.perf_counter() - started, 1)
        results.append(entry)

        if "error" in entry:
            print(f"[{number}/{len(questions)}] ERROR  {question!r}: {entry['error']}")
        else:
            s = entry["summary"]
            print(
                f"[{number}/{len(questions)}] intent={s.get('intent')} answerable={s.get('answerable')} "
                f"schema_ok={s.get('schema_ok')} tables={s.get('schema_tables')} rows={s.get('row_count')} "
                f"escalated={s.get('escalated')} answer_type={s.get('answer_type')} "
                f"{entry['seconds']}s  {question!r}"
            )
        if delay > 0 and number < len(questions):
            await asyncio.sleep(delay)

    return {
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Saving, loading, comparing
# ---------------------------------------------------------------------------

def result_path(label: str) -> Path:
    safe = "".join(ch for ch in label if ch.isalnum() or ch in "-_.")
    if not safe:
        raise ValueError("label must contain letters, digits, '-', '_' or '.'")
    return RESULTS_DIR / f"{safe}.json"


def save(run: Dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = result_path(run["label"])
    path.write_text(
        json.dumps(run, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def load(label: str) -> Optional[Dict[str, Any]]:
    path = result_path(label)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compare(old: Dict[str, Any], new: Dict[str, Any]) -> None:
    old_by_question = {r["question"]: r for r in old["results"]}
    print(f"\n=== Compare: {old['label']} (before)  ->  {new['label']} (after) ===")
    for entry in new["results"]:
        question = entry["question"]
        before = old_by_question.get(question)
        if before is None:
            print(f"NEW      {question!r} (not in the old run)")
            continue
        if "error" in entry or "error" in before:
            print(f"ERROR    {question!r}: before={before.get('error')} after={entry.get('error')}")
            continue

        diffs = []
        for field in COMPARE_FIELDS:
            was, now = before["summary"].get(field), entry["summary"].get(field)
            if was != now:
                diffs.append((field, was, now))
        if not diffs:
            print(f"SAME     {question!r}")
            continue
        print(f"CHANGED  {question!r}")
        for field, was, now in diffs:
            print(f"           {field}: {was!r}  ->  {now!r}")

    print("\n=== Verified row counts ===")
    for entry in new["results"]:
        expected = EXPECTED_ROW_COUNTS.get(entry["question"])
        if expected is None or "summary" not in entry:
            continue
        actual = entry["summary"].get("row_count")
        verdict = "OK  " if actual == expected else "FAIL"
        print(f"{verdict} expected={expected} actual={actual}  {entry['question']!r}")
    print(
        "\nA CHANGED line is not automatically bad. Check the rows against your "
        "reference SQL, then decide: keep the change or undo it."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main(args: argparse.Namespace) -> int:
    old = None
    if args.compare:
        old = load(args.compare)
        if old is None:
            print(f"No saved run named {args.compare!r} in {RESULTS_DIR}")
            return 2

    questions = QUESTIONS
    if args.only:
        questions = [q for q in QUESTIONS if args.only.lower() in q.lower()]
        if not questions:
            print(f"No question contains {args.only!r}")
            return 2

    try:
        await startup()
    except Exception as exc:
        print(f"Startup failed: {type(exc).__name__}: {exc}")
        return 1

    try:
        run = await run_all(args.label, args.delay, questions)
    finally:
        await close_mysql_connection()

    path = save(run)
    print(f"\nSaved: {path}")
    if old is not None:
        compare(old, run)
    return 0


if __name__ == "__main__":
    # Tamil and other non-English text must print on a Windows console.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="Run the regression questions.")
    parser.add_argument("--label", required=True, help="name for this run, e.g. baseline")
    parser.add_argument("--compare", help="label of an earlier run to compare against")
    parser.add_argument("--only", help="run only questions containing this text")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="seconds to wait between questions (default 3, helps with rate limits)")
    sys.exit(asyncio.run(main(parser.parse_args())))