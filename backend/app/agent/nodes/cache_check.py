"""
cache_check.py  (Node 2: Semantic Cache Gate)

Two levels, safest first:
  1. EXACT: same rewritten question after normalization. Cannot be wrong,
     works in any language, needs no embedding call.
  2. SEMANTIC: near-identical meaning (very high similarity) AND identical numbers.

A wrong instant answer is worse than a slow correct one, so when unsure this
cache says "miss" and lets the full pipeline run.

In-memory only: resets on restart and is NOT shared across worker processes.
Use Redis if you deploy with more than one worker.
NOTE: the cache is global. If different users may see different data,
add the user/tenant id to the cache key before real users arrive.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

import numpy as np

from app.services.embedding_service import embed_single_text, EmbeddingServiceError

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.97   # was 0.90: too loose, "shops in total" matched "shops with website"
NEAR_MISS_LOG_FLOOR = 0.85    # log rejected candidates above this, to tune the threshold with real data
MAX_CACHE_SIZE = 200
CACHE_TTL_SECONDS = 3600      # results are live data; lower this if your data changes fast
CACHE_ENTRY_VERSION = 5

# Each entry: {"question", "key", "embedding", "final_answer", "timestamp"}
_cache: List[Dict[str, Any]] = []

_NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)*")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)   # \w is Unicode-aware: works for any language
_SPACES = re.compile(r"\s+")


def _numbers_in(text: str) -> list:
    """Every number in the text, sorted, so two questions can be compared on numbers alone."""
    return sorted(_NUMBER_PATTERN.findall(text))


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse spaces. Used for the exact-match key."""
    text = _NON_WORD.sub(" ", text.casefold())
    return _SPACES.sub(" ", text).strip()


def _prune() -> None:
    """Drop expired or obsolete entries and enforce max size."""
    now = time.time()
    global _cache
    _cache = [
        e for e in _cache
        if now - e["timestamp"] < CACHE_TTL_SECONDS
        and e.get("answer_version") == CACHE_ENTRY_VERSION
    ]
    if len(_cache) > MAX_CACHE_SIZE:
        _cache = _cache[-MAX_CACHE_SIZE:]


async def check_cache(
    question: str, context_version: str = "default"
) -> Optional[Dict[str, Any]]:
    """
    Returns the cached final_answer dict on a safe hit, else None.
    Never raises: a broken embedding call degrades to "cache miss".
    """
    _prune()
    if not _cache:
        return None
    context_entries = [
        entry
        for entry in _cache
        if entry.get("context_version", "default") == context_version
    ]
    if not context_entries:
        return None

    # --- Level 1: exact match on the normalized question ---
    key = _normalize(question)
    for entry in reversed(context_entries):  # newest first
        if entry["key"] == key:
            logger.info("[cache_check] EXACT HIT for %r", question)
            return entry["final_answer"]

    # --- Level 2: strict semantic match ---
    try:
        query_emb = await embed_single_text(question)
    except EmbeddingServiceError as e:
        logger.warning("[cache_check] embedding failed, skipping semantic check: %s", e)
        return None

    best_score, best_entry = -1.0, None
    for entry in context_entries:
        score = float(np.dot(query_emb, entry["embedding"]))  # both pre-normalized
        if score > best_score:
            best_score, best_entry = score, entry

    if best_entry is None:
        return None

    numbers_match = _numbers_in(question) == _numbers_in(best_entry["question"])

    if best_score >= SIMILARITY_THRESHOLD and numbers_match:
        logger.info(
            "[cache_check] SEMANTIC HIT (score=%.4f) for %r -> matched %r",
            best_score, question, best_entry["question"],
        )
        return best_entry["final_answer"]

    if best_score >= NEAR_MISS_LOG_FLOOR:
        logger.info(
            "[cache_check] near-miss REJECTED (score=%.4f, numbers_match=%s) for %r vs %r",
            best_score, numbers_match, question, best_entry["question"],
        )
    return None


async def store(
    question: str,
    final_answer: Dict[str, Any],
    context_version: str = "default",
) -> None:
    """
    Caches a question/answer pair. Caller (Node 10) must only call this for
    answers that are safe to replay: graded-valid results or confirmed
    unanswerable verdicts. Never for escalations caused by transient failures.
    """
    if final_answer.get("answer_type") == "empty":
        logger.info("[cache_check] not caching empty answer for %r", question)
        return

    try:
        query_emb = await embed_single_text(question)
    except EmbeddingServiceError as e:
        logger.warning("[cache_check] embedding failed, not caching %r: %s", question, e)
        return

    _cache.append({
        "answer_version": CACHE_ENTRY_VERSION,
        "context_version": context_version,
        "question": question,
        "key": _normalize(question),
        "embedding": query_emb,
        "final_answer": final_answer,
        "timestamp": time.time(),
    })
    _prune()
    logger.info("[cache_check] stored %r (%d entries)", question, len(_cache))