"""Opt-in regression benchmark: question -> full pipeline -> executed result
compared with a golden SQL result.

Run from ``backend`` with:
    python -m app.tests.regression_benchmark
    python -m app.tests.regression_benchmark --cases 1,5-8 --delay 8

This contacts the configured LLM services and the read-only MySQL replica.
Generated SQL is safety-validated, checked with EXPLAIN, then executed. Golden
SQL must be a single SELECT statement. Results go to a separate CSV so the
existing benchmark evidence is preserved.

Scope: the 20 data cases. The 5 attack cases are NOT included yet; they must be
written as query PLANS (the safety step receives plans, not raw SQL) once
``safety_check.py`` has been reviewed.

Result labels
    Pass: EXACT, PASS_EXTRA_COLUMNS, PASS_PARTIAL_COLUMNS
    Weak pass:  PASS_EMPTY (both empty), SUPERSET (all golden rows plus more)
    Fail:       SUBSET, ROW_COUNT_DIFF, VALUE_DIFF, MISSING_COLUMNS, REFUSED,
                NOT_DATA, ERROR, GOLDEN_ERROR
    Re-run:     RATE_LIMITED (Groq 429; not counted as a failure)
"""

import argparse
import asyncio
import csv
import datetime
import decimal
import logging
import re
import time
from collections import Counter
from app.agent.nodes.generate_query import repair_query_after_safety_rejection
from app.agent.nodes.safety_check import REPAIRABLE_SAFETY_RULES
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from app.agent.nodes.business_rules import find_relevant_rules
from app.agent.nodes.classifier_rewrite import classify_and_rewrite
from app.agent.nodes.generate_query import (
    QueryGenerationError,
    UnanswerableQuestionError,
    generate_query,
    preflight_refusals,
)
from app.agent.nodes.grade_sample import (
    GradeSampleError,
    MissingRelationshipError,
    grade_schema_sufficiency,
)
from app.agent.nodes.return_save import _build_result_answer
from app.agent.nodes.run_self_fix import run_query
from app.agent.nodes.safety_check import SafetyViolationError, run_safety_check
from app.agent.nodes.schema_grounding import (
    get_cached_schema,
    refresh_schema_cache,
)
from app.db.mysql_client import (
    close_mysql_connection,
    connect_to_mysql,
    connect_to_read_replica,
    fetch_all,
)
from app.services.embedding_service import build_schema_index
from app.tests.query_generation_benchmark import (
    _execution_columns,
    _explain_query,
    _json,
    _parse_case_numbers,
    _retrieval_metrics,
    _safe_execution_preview,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# CASE CATALOG (20 data cases)
# golden_status: "confirmed" = expected answer trusted
#                "provisional" = depends on an open business decision (see note)
# ==============================================================================
def _case(
    number: int,
    category: str,
    question: str,
    golden_tables: Sequence[str],
    golden_sql: str,
    *,
    golden_status: str = "confirmed",
    note: str = "",
) -> Dict[str, Any]:
    return {
        "number": number,
        "category": category,
        "question": question,
        "golden_tables": list(golden_tables),
        "golden_sql": golden_sql,
        "golden_status": golden_status,
        "note": note,
    }


CASES: List[Dict[str, Any]] = [
    # ---------------- PART 1: fuzzy item match (1-10) ----------------
    _case(1, "fuzzy_item", "where can I get a mojito?",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%mojito%' OR s.name LIKE '%mojito%';"),
    _case(2, "fuzzy_item", "who has rose milk offers?",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%rose milk%' OR s.name LIKE '%rose milk%';"),
    _case(3, "fuzzy_item", "shops that sell muttai kuzhi paniyaram",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%muttai kuzhi paniyaram%' OR s.name LIKE '%muttai kuzhi paniyaram%';"),
    _case(4, "fuzzy_item", "looking for evening snacks",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%evening snacks%' OR o.offer_heading LIKE '%snacks%';"),
    _case(5, "fuzzy_item", "where can i learn ui ux design?",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%uiux%' OR o.offer_heading LIKE '%ui ux%';"),
    _case(6, "fuzzy_item", "looking for someone who builds houses",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%கட்டி தரப்படும்%';"),
    _case(7, "fuzzy_item", "who makes e-commerce apps?",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%e-commerce app%' OR o.offer_heading LIKE '%ecommerce app%';"),
    _case(8, "fuzzy_item", "is anyone selling a used iphone 12?",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%iphone 12%' AND o.offer_heading LIKE '%used%';"),
    _case(9, "fuzzy_item", "where to buy cupboards",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%cuboard%' OR o.offer_heading LIKE '%cupboard%';"),
    _case(10, "fuzzy_item", "who is selling samsung s23 ultra?",
          ["shop_details", "offer"],
          "SELECT DISTINCT s.name, o.offer_heading, o.offer_price FROM shop_details s JOIN offer o ON s.id = o.shop_id WHERE o.offer_heading LIKE '%s23ulra%' OR o.offer_heading LIKE '%s23 ultra%';"),
    # ---------------- PART 2: moderate and complex joins (11-20) ----------------
    _case(11, "complex_join",
          "Find shop name, address, category, and average rating for shops in 'Food & Dining'.",
          ["shop_details", "categories", "shop_address", "reviews"],
          "SELECT s.name, a.city, c.categoriesname, AVG(r.rating) as avg_rating FROM shop_details s JOIN categories c ON s.category_id = c.id JOIN shop_address a ON a.shop_id = s.id JOIN reviews r ON s.id = r.shop_id WHERE c.categoriesname = 'Food & Dining' AND a.is_default = 1 GROUP BY s.id, s.name, a.city, c.categoriesname ORDER BY s.id LIMIT 10;",
          note="Golden has LIMIT 10 but the question asks for no limit; check for a default limit if row counts differ."),
    _case(12, "complex_join", "Active offers in 'Nagercoil' under 'Services' category.",
          ["shop_details", "offer", "shop_address", "categories"],
          "SELECT s.name, o.offer_heading, c.categoriesname FROM shop_details s JOIN offer o ON s.id = o.shop_id JOIN shop_address a ON a.shop_id = s.id JOIN categories c ON s.category_id = c.id WHERE c.categoriesname = 'Services' AND a.city = 'Nagercoil' AND a.is_default = 1 AND o.status = 1;",
          note="Active offer = offer.status = 1 (team decision)."),
    _case(13, "complex_join",
          "List shops, subcategory, and lowest offer price where offer is less than 1000.",
          ["shop_details", "subcategories", "offer"],
          "SELECT s.name, sc.subcategoryname, MIN(o.offer_price) as lowest_offer FROM shop_details s JOIN subcategories sc ON s.subcategory_id = sc.id JOIN offer o ON s.id = o.shop_id GROUP BY s.id, s.name, sc.subcategoryname HAVING MIN(o.offer_price) < 1000 ORDER BY lowest_offer ASC LIMIT 10;",
          note="Ties on lowest_offer can make the LIMIT 10 rows unstable."),
    _case(14, "complex_join", "Show shop name, category name, total bookings, and average rating.",
          ["shop_details", "categories", "bookings", "reviews"],
          "SELECT s.name, c.categoriesname, COALESCE(b_count.total_bookings, 0) as total_bookings, r_avg.avg_rating FROM shop_details s JOIN categories c ON s.category_id = c.id LEFT JOIN (SELECT shop_id, COUNT(id) as total_bookings FROM bookings GROUP BY shop_id) b_count ON s.id = b_count.shop_id LEFT JOIN (SELECT shop_id, AVG(rating) as avg_rating FROM reviews GROUP BY shop_id) r_avg ON s.id = r_avg.shop_id WHERE b_count.total_bookings > 0 ORDER BY s.id LIMIT 10;",
          golden_status="provisional",
          note="Golden drops shops with zero bookings (WHERE total_bookings > 0), which the question does not ask for."),
    _case(15, "complex_join",
          "Shops with 'Juice' in name, their default city, number of offers, and average rating.",
          ["shop_details", "shop_address", "offer", "reviews"],
          "SELECT s.name, a.city, (SELECT COUNT(id) FROM offer WHERE shop_id = s.id) as offer_count, (SELECT AVG(rating) FROM reviews WHERE shop_id = s.id) as avg_rating FROM shop_details s JOIN shop_address a ON a.shop_id = s.id WHERE s.name LIKE '%Juice%' AND a.is_default = 1 ORDER BY s.id LIMIT 10;"),
    _case(16, "complex_join",
          "Find the highest discounted offer in rupees in 'Mobiles & Electronics' in 'Nagercoil'.",
          ["shop_details", "offer", "categories", "shop_address"],
          "SELECT s.name, o.offer_heading, (o.actual_price - o.offer_price) as discount FROM shop_details s JOIN offer o ON s.id = o.shop_id JOIN categories c ON s.category_id = c.id JOIN shop_address a ON a.shop_id = s.id WHERE s.status = 1 AND c.categoriesname = 'Mobiles & Electronics' AND a.city = 'Nagercoil' AND a.is_default = 1 AND o.offer_price > 0 AND o.actual_price > o.offer_price ORDER BY discount DESC LIMIT 10;",
          golden_status="provisional",
          note="Uses positive fixed-price savings, includes valid one-unit offer prices, and excludes zero-price giveaway rows."),
    _case(17, "complex_join", "Total booking revenue for shops with websites.",
          ["shop_details", "categories", "subcategories", "shop_address", "bookings"],
          "SELECT s.name, c.categoriesname, sc.subcategoryname, a.city, SUM(b.final_amount) as total_revenue FROM shop_details s JOIN categories c ON s.category_id = c.id JOIN subcategories sc ON s.subcategory_id = sc.id JOIN shop_address a ON a.shop_id = s.id JOIN bookings b ON s.id = b.shop_id WHERE s.websitelink IS NOT NULL AND s.websitelink != '' AND a.is_default = 1 GROUP BY s.id, s.name, c.categoriesname, sc.subcategoryname, a.city ORDER BY total_revenue DESC;"),
    _case(18, "complex_join",
          "Subcategories in Thiruvananthapuram with at least one shop rated above 4.5.",
          ["subcategories", "shop_details", "shop_address", "reviews"],
          "SELECT sc.subcategoryname, COUNT(DISTINCT s.id) as shop_count FROM subcategories sc JOIN shop_details s ON sc.id = s.subcategory_id JOIN shop_address a ON a.shop_id = s.id JOIN reviews r ON s.id = r.shop_id WHERE a.city = 'Thiruvananthapuram' AND a.is_default = 1 GROUP BY sc.id, sc.subcategoryname HAVING AVG(r.rating) > 4.5;",
          golden_status="provisional",
          note="Golden averages ratings per subcategory, not per shop. Shops are mostly around Nagercoil, so both results are probably empty (PASS_EMPTY)."),
    _case(19, "complex_join", "Categories and the count of unique cities they operate in.",
          ["categories", "shop_details", "shop_address"],
          "SELECT c.categoriesname, COUNT(DISTINCT a.city) as unique_cities FROM categories c JOIN shop_details s ON c.id = s.category_id JOIN shop_address a ON a.shop_id = s.id WHERE a.is_default = 1 GROUP BY c.id, c.categoriesname;"),
    _case(20, "complex_join", "Max and Min offer price for shops in the 'Fashions' category.",
          ["shop_details", "shop_address", "offer", "categories"],
          "SELECT s.name, a.city, MAX(o.offer_price) as max_offer, MIN(o.offer_price) as min_offer FROM shop_details s JOIN shop_address a ON a.shop_id = s.id JOIN offer o ON s.id = o.shop_id JOIN categories c ON s.category_id = c.id WHERE c.categoriesname = 'Fashions' AND a.is_default = 1 GROUP BY s.id, s.name, a.city ORDER BY s.id LIMIT 10;"),
]


CSV_FIELDS = [
    "case_number", "category", "question", "rewritten_question",
    "golden_status", "result", "passed", "failure_group", "failed_stage",
    "attempts", "total_ms", "classification_ms", "schema_grade_ms",
    "generation_ms", "sql_validation_ms", "execution_ms", "golden_ms",
    "schema_attempts", "selected_schema_tables", "golden_tables",
    "missing_tables", "grounded_table_recall", "matched_business_rules",
    "compiled_sql", "sql_parameters", "golden_sql",
    "safety_validation", "explain_validation",
    "generated_row_count", "golden_row_count",
    "generated_columns", "golden_columns", "truncated", "no_data_reason",
    "diff_detail", "hint", "generated_preview", "golden_preview",
    "answer_type", "answer_message", "note", "error", "error_type",
]

PASS_LABELS = {"EXACT", "PASS_EXTRA_COLUMNS", "PASS_PARTIAL_COLUMNS"}
WEAK_LABELS = {"PASS_EMPTY", "SUPERSET"}


# ==============================================================================
# RESULT COMPARISON (plain Python, no pandas)
# ==============================================================================
def _norm_value(value: Any) -> Any:
    """Make values from two queries comparable (Decimal vs float, padding, dates)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, decimal.Decimal)):
        return round(float(value), 2)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value).strip()


def _rows_as_dicts(rows: Optional[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Accept dict rows or tuple rows (tuples get generic column names)."""
    converted: List[Dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            converted.append(row)
        else:
            converted.append({f"col{i}": v for i, v in enumerate(row)})
    return converted


def _to_matrix(rows: Sequence[Dict[str, Any]]) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    if not rows:
        return [], []
    columns = list(rows[0].keys())
    return columns, [
        tuple(_norm_value(row.get(column)) for column in columns) for row in rows
    ]


def _iter_mappings(candidates: List[List[int]], limit: int = 200) -> Iterator[List[int]]:
    """Yield ways to pair each golden column with a different generated column."""
    produced = 0

    def walk(position: int, used: Set[int], chosen: List[int]) -> Iterator[List[int]]:
        nonlocal produced
        if produced >= limit:
            return
        if position == len(candidates):
            produced += 1
            yield list(chosen)
            return
        for index in candidates[position]:
            if index in used:
                continue
            used.add(index)
            chosen.append(index)
            yield from walk(position + 1, used, chosen)
            chosen.pop()
            used.discard(index)

    yield from walk(0, set(), [])


def _relation(generated: Counter, golden: Counter) -> str:
    if generated == golden:
        return "equal"
    if all(generated[key] >= count for key, count in golden.items()):
        return "superset"
    if all(golden[key] >= count for key, count in generated.items()):
        return "subset"
    return "other"


def _compare_projected_distinct(
    gen_cols: List[str],
    gen_rows: List[Tuple[Any, ...]],
    gold_cols: List[str],
    gold_rows: List[Tuple[Any, ...]],
) -> Optional[Tuple[str, str]]:
    """Generated query returned fewer columns than golden: compare only those
    columns, on distinct rows. Returns None if no column pairing is possible."""
    candidates: List[List[int]] = []
    for i in range(len(gen_cols)):
        gen_values = {row[i] for row in gen_rows}
        candidates.append([
            j for j in range(len(gold_cols))
            if gen_values & {row[j] for row in gold_rows}
        ])

    gen_distinct = Counter(set(gen_rows))
    rank = {"equal": 0, "superset": 1, "subset": 2, "other": 3}
    best: Optional[Tuple[int, str, Counter]] = None
    for mapping in _iter_mappings(candidates):
        gold_distinct = Counter({tuple(row[j] for j in mapping) for row in gold_rows})
        relation = _relation(gen_distinct, gold_distinct)
        if best is None or rank[relation] < best[0]:
            best = (rank[relation], relation, gold_distinct)
        if relation == "equal":
            break
    if best is None:
        return None

    _, relation, gold_distinct = best
    detail = (
        f"compared on {len(gen_cols)} generated column(s), distinct rows: "
        f"generated={sum(gen_distinct.values())} golden={sum(gold_distinct.values())} "
        f"only_in_generated={sum((gen_distinct - gold_distinct).values())} "
        f"only_in_golden={sum((gold_distinct - gen_distinct).values())}"
    )
    if relation == "equal":
        return "PASS_DISTINCT_MATCH", detail
    if relation == "superset":
        return "SUPERSET", detail
    if relation == "subset":
        return "SUBSET", detail
    return None


def _iter_partial_mappings(
    candidates: List[List[int]], limit: int = 500
) -> Iterator[List[Optional[int]]]:
    """Yield ways to pair generated columns with golden columns.

    Each generated column maps to a different golden column or to None
    (ignored). At least one column must be matched.
    """
    produced = 0

    def walk(
        position: int, used: Set[int], chosen: List[Optional[int]]
    ) -> Iterator[List[Optional[int]]]:
        nonlocal produced
        if produced >= limit:
            return
        if position == len(candidates):
            if any(item is not None for item in chosen):
                produced += 1
                yield list(chosen)
            return
        for index in candidates[position]:
            if index in used:
                continue
            used.add(index)
            chosen.append(index)
            yield from walk(position + 1, used, chosen)
            chosen.pop()
            used.discard(index)
        chosen.append(None)
        yield from walk(position + 1, used, chosen)
        chosen.pop()

    yield from walk(0, set(), [])


def _compare_partial_columns(
    gen_cols: List[str],
    gen_rows: List[Tuple[Any, ...]],
    gold_cols: List[str],
    gold_rows: List[Tuple[Any, ...]],
) -> Optional[Tuple[str, str]]:
    """Compare only the columns the generated query returned.

    Columns are matched by value (golden columns have no real names). Rows are
    compared as sets, so duplicate golden rows (for example one shop with many
    offers) do not count against a query that returns each shop once.
    Returns None when no generated column lines up with a golden column.
    """
    gen_sets = [{row[i] for row in gen_rows} for i in range(len(gen_cols))]
    gold_sets = [{row[j] for row in gold_rows} for j in range(len(gold_cols))]
    candidates: List[List[int]] = [
        [
            j for j, gold_set in enumerate(gold_sets)
            if gen_set <= gold_set or gold_set <= gen_set
        ]
        for gen_set in gen_sets
    ]

    rank = {"equal": 0, "superset": 1, "subset": 2, "other": 3}
    best: Optional[Tuple[Tuple[int, int], str, List[Tuple[int, int]]]] = None
    for mapping in _iter_partial_mappings(candidates):
        pairs = [(i, j) for i, j in enumerate(mapping) if j is not None]
        gen_proj = Counter({tuple(row[i] for i, _ in pairs) for row in gen_rows})
        gold_proj = Counter({tuple(row[j] for _, j in pairs) for row in gold_rows})
        relation = _relation(gen_proj, gold_proj)
        score = (rank[relation], -len(pairs))
        if best is None or score < best[0]:
            best = (score, relation, pairs)
        if score == (0, -len(gen_cols)):
            break

    if best is None or best[1] == "other":
        return None
    _, relation, pairs = best
    matched = [gen_cols[i] for i, _ in pairs]
    ignored = [gen_cols[i] for i in range(len(gen_cols)) if i not in {p[0] for p in pairs}]
    detail = (
        f"partial-column compare on {matched} (distinct rows); "
        f"ignored={ignored}; golden columns={len(gold_cols)}"
    )
    label = {
        "equal": "PASS_PARTIAL_COLUMNS",
        "superset": "SUPERSET",
        "subset": "SUBSET",
    }[relation]
    return label, detail

def _compare_results(
    generated: Sequence[Dict[str, Any]], golden: Sequence[Dict[str, Any]]
) -> Tuple[str, str]:
    """Compare two result sets ignoring row order, column order and column names."""
    gen_cols, gen_rows = _to_matrix(generated)
    gold_cols, gold_rows = _to_matrix(golden)

    if not gen_rows and not gold_rows:
        return "PASS_EMPTY", "both results are empty"
    if not gold_rows:
        return "ROW_COUNT_DIFF", f"golden is empty, generated has {len(gen_rows)} rows"
    if not gen_rows:
        return "ROW_COUNT_DIFF", f"generated is empty, golden has {len(gold_rows)} rows"

    same_row_count = len(gen_rows) == len(gold_rows)
    gen_counters = [
        Counter(row[i] for row in gen_rows) for i in range(len(gen_cols))
    ]
    candidates: List[List[int]] = []
    for j in range(len(gold_cols)):
        gold_counter = Counter(row[j] for row in gold_rows)
        candidates.append([
            i for i, gen_counter in enumerate(gen_counters)
            if (
                gen_counter == gold_counter
                if same_row_count
                else all(gen_counter[k] >= c for k, c in gold_counter.items())
            )
        ])

    gold_multiset = Counter(gold_rows)
    best: Optional[Tuple[int, str, Counter]] = None
    rank = {"equal": 0, "superset": 1, "subset": 2, "other": 3}
    for mapping in _iter_mappings(candidates):
        projected = Counter(
            tuple(row[i] for i in mapping) for row in gen_rows
        )
        relation = _relation(projected, gold_multiset)
        if best is None or rank[relation] < best[0]:
            best = (rank[relation], relation, projected)
        if relation == "equal":
            break

    if best is not None:
        _, relation, projected = best
        only_gen = sum((projected - gold_multiset).values())
        only_gold = sum((gold_multiset - projected).values())
        detail = (
            f"rows generated={len(gen_rows)} golden={len(gold_rows)} "
            f"only_in_generated={only_gen} only_in_golden={only_gold}"
        )
        if relation == "equal":
            label = "EXACT" if len(gen_cols) == len(gold_cols) else "PASS_EXTRA_COLUMNS"
            return label, detail
        if relation == "superset":
            return "SUPERSET", detail
        if relation == "subset":
            return "SUBSET", detail
        return ("VALUE_DIFF" if same_row_count else "ROW_COUNT_DIFF"), detail

    partial = _compare_partial_columns(gen_cols, gen_rows, gold_cols, gold_rows)
    if partial is not None:
        return partial
    
    if len(gen_cols) < len(gold_cols):
        projected = _compare_projected_distinct(
            gen_cols, gen_rows, gold_cols, gold_rows
        )
        if projected is not None:
            return projected
        return "MISSING_COLUMNS", (
            f"golden has {len(gold_cols)} columns, generated has {len(gen_cols)}; "
            f"generated_columns={gen_cols}"
        )
    if len(gen_cols) == len(gold_cols):
        direct = _relation(Counter(gen_rows), gold_multiset)
        only_gen = sum((Counter(gen_rows) - gold_multiset).values())
        only_gold = sum((gold_multiset - Counter(gen_rows)).values())
        detail = (
            f"no column mapping; rows generated={len(gen_rows)} golden={len(gold_rows)} "
            f"only_in_generated={only_gen} only_in_golden={only_gold} direct={direct}"
        )
    else:
        detail = (
            f"no column mapping; rows generated={len(gen_rows)} "
            f"golden={len(gold_rows)}; generated_columns={gen_cols}"
        )
    return ("VALUE_DIFF" if same_row_count else "ROW_COUNT_DIFF"), detail


# ==============================================================================
# HELPERS
# ==============================================================================
def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)

_RATE_LIMIT_PATTERN = re.compile(r"\b429\b|rate[\s_-]?limit", re.IGNORECASE)

def _is_rate_limited(exc: Optional[BaseException]) -> bool:
    """True if the error (or anything that caused it) is a Groq/LLM 429."""
    seen: Set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if _RATE_LIMIT_PATTERN.search(f"{type(exc).__name__} {exc}"):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _prepare_golden_sql(sql: str) -> str:
    """Allow one read-only SELECT only. Escape % so LIKE patterns survive.

    aiomysql runs ``query % args`` when args is not None, so a bare '%' in
    ``LIKE '%x%'`` can raise. '%%' is correct if formatting happens, and still
    matches the same rows (two wildcards in a row) if it does not.
    """
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned.lower().startswith("select"):
        raise ValueError("golden SQL must start with SELECT")
    if ";" in cleaned:
        raise ValueError("golden SQL must be a single statement")
    return cleaned.replace("%", "%%")


def _hint(compiled_sql: str, golden_sql: str, truncated: Any) -> str:
    hints: List[str] = []
    status = re.compile(r"\bstatus\b", re.IGNORECASE)
    limit = re.compile(r"\blimit\b", re.IGNORECASE)
    if status.search(compiled_sql) and not status.search(golden_sql):
        hints.append("compiled SQL filters on status; golden SQL does not")
    if limit.search(golden_sql) and not limit.search(compiled_sql):
        hints.append("golden SQL has LIMIT; compiled SQL has none")
    if limit.search(compiled_sql) and not limit.search(golden_sql):
        hints.append("compiled SQL has LIMIT; golden SQL has none")
    if truncated:
        hints.append("generated result was truncated")
    return "; ".join(hints)


class NotDataIntent(RuntimeError):
    """The classifier did not route a data question to the data pipeline."""


def _record_failure(row: Dict[str, Any], exc: BaseException) -> None:
    row["error"] = f"{type(exc).__name__}: {exc}"
    row["error_type"] = type(exc).__name__
    if _is_rate_limited(exc):
        row["result"], row["passed"], row["failure_group"] = "RATE_LIMITED", "rerun", "rate_limit"
    elif isinstance(exc, NotDataIntent):
        row["result"], row["passed"], row["failure_group"] = "NOT_DATA", "no", "classifier"
    elif isinstance(exc, (UnanswerableQuestionError, MissingRelationshipError)):
        row["result"], row["passed"], row["failure_group"] = "REFUSED", "no", "refusal"
    elif isinstance(exc, GradeSampleError):
        row["result"], row["passed"], row["failure_group"] = "ERROR", "no", "schema"
    elif isinstance(exc, QueryGenerationError):
        row["result"], row["passed"], row["failure_group"] = "ERROR", "no", "generation"
    elif isinstance(exc, SafetyViolationError):
        row["safety_validation"] = "failed"
        row["result"], row["passed"], row["failure_group"] = "ERROR", "no", "safety"
    elif isinstance(exc, RuntimeError) and str(exc).startswith("MySQL EXPLAIN rejected"):
        row["explain_validation"] = "failed"
        row["result"], row["passed"], row["failure_group"] = "ERROR", "no", "sql_error"
    elif isinstance(exc, asyncio.TimeoutError):
        row["result"], row["passed"], row["failure_group"] = "ERROR", "no", "timeout"
    else:
        row["result"], row["passed"], row["failure_group"] = "ERROR", "no", "other"


# ==============================================================================
# ONE CASE
# ==============================================================================
def _new_row(case: Dict[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {field: "" for field in CSV_FIELDS}
    row.update({
        "case_number": case["number"],
        "category": case["category"],
        "question": case["question"],
        "golden_status": case["golden_status"],
        "golden_tables": _json(case["golden_tables"]),
        "golden_sql": case["golden_sql"],
        "note": case["note"],
        "result": "NOT_RUN",
        "passed": "no",
        "safety_validation": "not_run",
        "explain_validation": "not_run",
        "attempts": 0,
    })
    return row


async def _attempt(
    case: Dict[str, Any],
    row: Dict[str, Any],
    golden_dicts: List[Dict[str, Any]],
    query_timeout: float,
) -> None:
    """Run the pipeline once and fill the row. Raises on any failure."""
    row["error"] = row["error_type"] = ""

    row["failed_stage"] = "classify"
    stage = time.perf_counter()
    classification = await classify_and_rewrite(case["question"])
    row["classification_ms"] = _ms(stage)
    if classification.get("intent") != "data":
        raise NotDataIntent(
            f"Classifier routed this data case as {classification.get('intent')!r}."
        )
    question = classification["rewritten_question"]
    row["rewritten_question"] = question

    row["failed_stage"] = "preflight"
    preflight_refusals(question, case["question"])

    row["failed_stage"] = "schema_grade"
    stage = time.perf_counter()
    try:
        grounded = await grade_schema_sufficiency(question)
    finally:
        row["schema_grade_ms"] = _ms(stage)
    selected_tables = list(grounded["schema"])
    row["schema_attempts"] = grounded.get("attempts", "")
    row["selected_schema_tables"] = _json(selected_tables)
    _, recall, _ = _retrieval_metrics(selected_tables, case["golden_tables"])
    row["grounded_table_recall"] = round(recall, 4)
    row["missing_tables"] = _json(
        sorted(set(case["golden_tables"]) - set(selected_tables))
    )

    row["failed_stage"] = "business_rules"
    rules = find_relevant_rules(question)
    row["matched_business_rules"] = _json([rule.get("term") for rule in rules])

    row["failed_stage"] = "generate_query"
    stage = time.perf_counter()
    try:
        query = await generate_query(
            question,
            grounded["schema"],
            rules,
            grounded["relationships"],
            case["question"],
        )
    finally:
        row["generation_ms"] = _ms(stage)

    row["failed_stage"] = "safety_check"
    stage = time.perf_counter()
    try:
        try:
            safe_query = run_safety_check(
                query,
                valid_tables=selected_tables,
                schema_context=grounded["schema"],
                relationships=grounded["relationships"],
            )
        except SafetyViolationError as first_error:
            # Mirror graph.py: one repair attempt, only for repairable rules.
            if first_error.rule_id not in REPAIRABLE_SAFETY_RULES:
                raise
            query = await repair_query_after_safety_rejection(
                query,
                str(first_error),
                question,
                grounded["schema"],
                rules,
                grounded["relationships"],
                case["question"],
            )
            safe_query = run_safety_check(
                query,
                valid_tables=selected_tables,
                schema_context=grounded["schema"],
                relationships=grounded["relationships"],
            )
    finally:
        row["sql_validation_ms"] = _ms(stage)
    row["compiled_sql"] = safe_query["sql"]
    row["sql_parameters"] = _json(safe_query.get("params", []))
    row["safety_validation"] = "passed"

    row["failed_stage"] = "explain"
    await asyncio.wait_for(
        _explain_query(safe_query["sql"], safe_query.get("params", [])),
        timeout=query_timeout,
    )
    row["explain_validation"] = "passed"

    row["failed_stage"] = "execute"
    stage = time.perf_counter()
    try:
        execution = await asyncio.wait_for(
            run_query({
                **safe_query,
                "columns": _execution_columns(query, grounded["schema"]),
            }),
            timeout=query_timeout,
        )
    finally:
        row["execution_ms"] = _ms(stage)
    answer = _build_result_answer(execution, query)
    generated_dicts = _rows_as_dicts(execution["results"])
    row["generated_row_count"] = execution["row_count"]
    row["truncated"] = execution["truncated"]
    row["no_data_reason"] = execution.get("no_data_reason") or ""
    row["answer_type"] = answer["answer_type"]
    row["answer_message"] = answer.get("message", "")
    row["generated_columns"] = _json(
        list(generated_dicts[0].keys()) if generated_dicts else []
    )
    row["generated_preview"] = _json(_safe_execution_preview(generated_dicts))

    row["failed_stage"] = "compare"
    label, detail = _compare_results(generated_dicts, golden_dicts)
    row["result"] = label
    row["diff_detail"] = detail
    row["failed_stage"] = ""
    if label in PASS_LABELS:
        row["passed"], row["failure_group"] = "yes", ""
    elif label in WEAK_LABELS:
        row["passed"], row["failure_group"] = "weak", ""
    else:
        row["passed"] = "no"
        row["failure_group"] = (
            "wrong_tables"
            if json_list(row["missing_tables"])
            else "data_mismatch"
        )
        row["hint"] = _hint(
            row["compiled_sql"], case["golden_sql"], row["truncated"]
        )


def json_list(text: str) -> List[Any]:
    import json

    try:
        value = json.loads(text) if text else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


async def _run_case(
    case: Dict[str, Any], *, retries: int, retry_wait: float, query_timeout: float
) -> Dict[str, Any]:
    started = time.perf_counter()
    row = _new_row(case)

    # Golden first (once): its result does not change between retries.
    stage = time.perf_counter()
    golden_dicts: List[Dict[str, Any]] = []
    try:
        golden_sql = _prepare_golden_sql(case["golden_sql"])
        golden_dicts = _rows_as_dicts(
            await asyncio.wait_for(fetch_all(golden_sql, ()), timeout=query_timeout)
        )
        row["golden_row_count"] = len(golden_dicts)
        row["golden_columns"] = _json(
            list(golden_dicts[0].keys()) if golden_dicts else []
        )
        row["golden_preview"] = _json(_safe_execution_preview(golden_dicts))
    except Exception as exc:
        logger.exception("Golden SQL failed for case %s", case["number"])
        row["result"], row["passed"], row["failure_group"] = "GOLDEN_ERROR", "no", "golden_error"
        row["failed_stage"] = "golden"
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["error_type"] = type(exc).__name__
        row["golden_ms"] = _ms(stage)
        row["total_ms"] = _ms(started)
        return row
    row["golden_ms"] = _ms(stage)

    for attempt in range(1, retries + 2):
        row["attempts"] = attempt
        try:
            await _attempt(case, row, golden_dicts, query_timeout)
            break
        except Exception as exc:
            if _is_rate_limited(exc) and attempt <= retries:
                logger.warning(
                    "Case %s hit a rate limit; waiting %.0fs (attempt %d/%d)",
                    case["number"], retry_wait, attempt, retries + 1,
                )
                await asyncio.sleep(retry_wait)
                continue
            logger.exception("Regression case %s failed", case["number"])
            _record_failure(row, exc)
            break
    row["total_ms"] = _ms(started)
    return row


# ==============================================================================
# SUMMARY + MAIN
# ==============================================================================
def _print_summary(rows: List[Dict[str, Any]]) -> None:
    total = len(rows)
    rerun = [r for r in rows if r["passed"] == "rerun"]
    passed = [r for r in rows if r["passed"] == "yes"]
    weak = [r for r in rows if r["passed"] == "weak"]
    failed = [r for r in rows if r["passed"] == "no"]
    counted = total - len(rerun)
    latencies = [r["total_ms"] for r in rows if isinstance(r["total_ms"], (int, float))]

    print("\n" + "=" * 80)
    print("REGRESSION BENCHMARK SUMMARY")
    print("=" * 80)
    print(f"Cases run        : {total}")
    print(f"Pass             : {len(passed)}")
    print(f"Weak pass        : {len(weak)}  (both empty, or extra rows returned)")
    print(f"Fail             : {len(failed)}")
    print(f"Re-run (429)     : {len(rerun)}  (not counted as failures)")
    if counted:
        print(f"Pass rate        : {len(passed) / counted * 100:.1f}% of {counted} counted cases")
    if latencies:
        print(f"Average latency  : {sum(latencies) / len(latencies) / 1000:.2f}s per case")

    print("\nBy category (pass / weak / fail / re-run):")
    for category in sorted({r["category"] for r in rows}):
        subset = [r for r in rows if r["category"] == category]
        print(
            f"  {category:<14} "
            f"{sum(r['passed'] == 'yes' for r in subset)} / "
            f"{sum(r['passed'] == 'weak' for r in subset)} / "
            f"{sum(r['passed'] == 'no' for r in subset)} / "
            f"{sum(r['passed'] == 'rerun' for r in subset)}"
        )

    groups = Counter(r["failure_group"] for r in failed)
    if groups:
        print("\nFailures by cause:")
        for group, count in groups.most_common():
            ids = [str(r["case_number"]) for r in failed if r["failure_group"] == group]
            print(f"  {group:<14} {count}  (cases {', '.join(ids)})")
    if rerun:
        print("\nRe-run these: " + ", ".join(str(r["case_number"]) for r in rerun))
    provisional = [
        str(r["case_number"]) for r in rows
        if r["golden_status"] == "provisional" and r["passed"] != "yes"
    ]
    if provisional:
        print(
            "Provisional golden answers not passing (check the expected answer "
            "first): " + ", ".join(provisional)
        )
    print("=" * 80 + "\n")


async def _run(args: argparse.Namespace) -> None:
    output_path: Path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    selected = _parse_case_numbers(args.cases) if args.cases else None
    cases_to_run = [
        case for case in CASES if selected is None or case["number"] in selected
    ]
    if not cases_to_run:
        raise SystemExit(f"No benchmark cases match {sorted(selected or [])}.")

    rows: List[Dict[str, Any]] = []
    await connect_to_mysql()
    try:
        await connect_to_read_replica()
        await refresh_schema_cache()
        await build_schema_index(get_cached_schema())

        # File stays open for the whole run; each row is flushed to disk
        # immediately, so a crash or Ctrl-C keeps everything finished so far.
        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(
                csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore"
            )
            writer.writeheader()
            csv_file.flush()

            for index, case in enumerate(cases_to_run):
                if index and args.delay:
                    await asyncio.sleep(args.delay)
                logger.info(
                    "Regression case %d (%d/%d)",
                    case["number"], index + 1, len(cases_to_run),
                )
                row = await _run_case(
                    case,
                    retries=args.retries,
                    retry_wait=args.retry_wait,
                    query_timeout=args.query_timeout,
                )
                print(
                    f"Case {case['number']:02d} [{case['category']}] "
                    f"{row['result']:<18} {row['total_ms'] / 1000:.1f}s"
                    + (f"  {row['diff_detail']}" if row["diff_detail"] else "")
                    + (f"  ERROR: {row['error']}" if row["error"] else "")
                )
                rows.append(row)
                writer.writerow(row)
                csv_file.flush()

        print(f"\nWrote {len(rows)} rows to {output_path}")
        print("Generated SQL ran on the read-only replica. Preview values with identifiers,")
        print("names, contact details or amounts are redacted in the CSV.")
        _print_summary(rows)
    finally:
        await close_mysql_connection()

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).with_name("regression_benchmark_results.csv"),
        help="CSV destination (default: app/tests/regression_benchmark_results.csv)",
    )
    parser.add_argument(
        "--cases", type=str, default=None,
        help="Run only these case numbers, e.g. '1,5-8' (default: all)",
    )
    parser.add_argument(
        "--delay", type=float, default=5.0,
        help="Seconds to wait between cases to stay under rate limits (default: 5)",
    )
    parser.add_argument(
        "--retries", type=int, default=1,
        help="Retries for a case that hits a 429 rate limit (default: 1)",
    )
    parser.add_argument(
        "--retry-wait", type=float, default=30.0,
        help="Seconds to wait before retrying after a 429 (default: 30)",
    )
    parser.add_argument(
        "--query-timeout", type=float, default=60.0,
        help="Seconds allowed for each database query (default: 60)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()