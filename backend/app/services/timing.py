"""
timing.py — per-request timing capture for every graph node and LLM call.

Uses a ContextVar (a value scoped to one request) so LLM calls made inside
a node are automatically attributed to that node, with no extra arguments.
"""

import functools
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional


logger = logging.getLogger(__name__)

_current_node: ContextVar[str] = ContextVar("current_node", default="-")
_events: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("timing_events", default=None)

_emitter: ContextVar[Optional[Callable[[Dict[str, Any]], None]]] = ContextVar(
    "timing_emitter", default=None
)


def set_emitter(fn: Callable[[Dict[str, Any]], None]) -> None:
    """Register a callback that receives live node_start / node_done events."""
    _emitter.set(fn)


def _emit(event: Dict[str, Any]) -> None:
    fn = _emitter.get()
    if fn is None:
        return
    try:
        fn(event)
    except Exception:
        # A broken listener must never break the pipeline.
        logger.exception("timing emitter failed")

def start_request() -> List[Dict[str, Any]]:
    """Call once per request, before the graph runs. Returns the shared event list."""
    events: List[Dict[str, Any]] = []
    _events.set(events)
    return events


def _append(event: Dict[str, Any]) -> None:
    events = _events.get()
    if events is not None:
        events.append(event)


def record_llm_call(
    role: str,
    provider: str,
    model: str,
    seconds: float,
    attempts: int,
    causes: List[str],
    backoff_seconds: float,
    ok: bool,
) -> None:
    node = _current_node.get()
    _append({
        "kind": "llm", "node": node, "role": role, "provider": provider,
        "model": model, "seconds": seconds, "attempts": attempts,
        "causes": list(causes), "backoff_seconds": backoff_seconds, "ok": ok,
    })
    logger.info(
        "LLM_CALL node=%s role=%s model=%s took=%.1fs attempts=%d backoff=%.1fs causes=%s ok=%s",
        node, role, model, seconds, attempts, backoff_seconds, causes or "none", ok,
    )


def record_database_call(
    operation: str,
    pool_wait_seconds: float,
    query_fetch_seconds: float,
    row_count: Optional[int],
    ok: bool,
) -> None:
    _append({
        "kind": "database",
        "stage": _current_node.get(),
        "operation": operation,
        "pool_wait_seconds": pool_wait_seconds,
        "query_fetch_seconds": query_fetch_seconds,
        "row_count": row_count,
        "ok": ok,
    })


@contextmanager
def timing_scope(name: str) -> Iterator[None]:
    """Capture a named stage and attribute nested LLM/DB calls to it."""
    token = _current_node.set(name)
    start = time.monotonic()
    try:
        yield
    finally:
        seconds = time.monotonic() - start
        _append({"kind": "node", "name": name, "seconds": seconds})
        logger.info("NODE_DONE node=%s took=%.1fs", name, seconds)
        _current_node.reset(token)


def timed_node(name: str, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wraps an async graph node so its duration is recorded and streamed."""

    @functools.wraps(fn)
    async def wrapper(state):
        _emit({"type": "node_start", "node": name})
        start = time.monotonic()
        try:
            with timing_scope(name):
                return await fn(state)
        finally:
            _emit({
                "type": "node_done",
                "node": name,
                "seconds": round(time.monotonic() - start, 1),
            })

    return wrapper

def format_timing_report(events: List[Dict[str, Any]]) -> str:
    """
    One row per node:
      total   = whole node time
      llm     = time inside LLM calls (includes retry waits)
      wait    = part of llm spent sleeping between retries
      other   = total - llm  (DB, local embeddings, Python code)
    """
    nodes = [e for e in events if e["kind"] == "node"]
    calls = [e for e in events if e["kind"] == "llm"]
    if not nodes:
        return "   (no timing data)"

    lines = [
        "   NODE TIMING",
        f"   {'node':<16}{'total':>8}{'llm':>8}{'wait':>8}{'other':>8}  llm calls",
    ]
    for n in nodes:
        mine = [c for c in calls if c["node"] == n["name"]]
        llm_total = sum(c["seconds"] for c in mine)
        wait_total = sum(c["backoff_seconds"] for c in mine)
        other = max(n["seconds"] - llm_total, 0.0)
        detail = ", ".join(
            f"{c['role']} {c['seconds']:.1f}s"
            + (f" [{'+'.join(c['causes'])}]" if c["causes"] else "")
            for c in mine
        ) or "-"
        lines.append(
            f"   {n['name']:<16}{n['seconds']:>7.1f}s{llm_total:>7.1f}s"
            f"{wait_total:>7.1f}s{other:>7.1f}s  {detail}"
        )
    total = sum(n["seconds"] for n in nodes)
    lines.append(f"   {'TOTAL':<16}{total:>7.1f}s")
    return "\n".join(lines)