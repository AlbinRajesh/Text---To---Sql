"""Golden Benchmark Catalog 5 (Cases 73-78).

Focused catalog and clarification regressions. The master runner executes the
SQL and clarification cases; API-only multi-turn checks are documented as
manual diagnostics because the runner invokes the graph directly.
"""

from typing import Any, Dict, List, Sequence


def _case(
    number: int,
    category: str,
    question: str,
    golden_tables: Sequence[str] = (),
    golden_sql: str = "",
    *,
    note: str,
    case_type: str = "sql",
    turns: Sequence[Dict[str, str]] = (),
    expected_outcome: str = "sql",
    expected_clarification: bool = False,
) -> Dict[str, Any]:
    return {
        "number": number,
        "category": category,
        "question": question,
        "golden_tables": list(golden_tables),
        "golden_sql": golden_sql.strip(),
        "expected_outcome": expected_outcome,
        "note": note,
        "case_type": case_type,
        "expected_intent": "",
        "expected_complexity": "",
        "expected_route": "",
        "turns": [dict(turn) for turn in turns],
        "expected_clarification": expected_clarification,
    }


GOLDEN_BENCHMARK_CASES: List[Dict[str, Any]] = [
    _case(
        73,
        "catalog_menu_value",
        "Show all active shops.",
        ["shop_details"],
        "SELECT s.name AS shop_name FROM shop_details s WHERE s.status = 1;",
        note=(
            "'All' means no category filter; it must not match the catalog's "
            "menu subcategory named All."
        ),
    ),
    _case(
        74,
        "percentage_query",
        "What percentage of reviews created in 2025 were rated five stars?",
        ["reviews"],
        "SELECT ROUND("
        "COUNT(CASE WHEN rating = 5 THEN 1 END) * 100.0 "
        "/ NULLIF(COUNT(*), 0), 1"
        ") AS five_star_percentage "
        "FROM reviews "
        "WHERE created_at >= '2025-01-01 00:00:00' "
        "AND created_at < '2026-01-01 00:00:00';",
        note=(
            "A computable percentage with a defined denominator and time range "
            "must be answered with SQL, not clarification."
        ),
    ),
    _case(
        75,
        "catalog_or_filter",
        "List active shops classified under Snacks & Beverages or "
        "Drinks & Beverages.",
        ["shop_details", "subcategories"],
        "SELECT DISTINCT s.name AS shop_name "
        "FROM shop_details s "
        "JOIN subcategories sc ON sc.id = s.subcategory_id "
        "WHERE s.status = 1 "
        "AND sc.subcategoryname IN "
        "('Snacks & Beverages', 'Drinks & Beverages');",
        note=(
            "Both terms are real subcategories. The OR request must retain "
            "both rather than silently dropping either one."
        ),
    ),
    _case(
        76,
        "catalog_and_filter",
        "List active shops classified under both the Snacks & Beverages "
        "and Drinks & Beverages subcategories.",
        case_type="conversation",
        turns=[{
            "question": (
                "List active shops classified under both the Snacks & "
                "Beverages and Drinks & Beverages subcategories."
            ),
        }],
        expected_outcome="clarification",
        expected_clarification=True,
        note=(
            "The AND request must not be rewritten as an OR or reduced to one "
            "subcategory; ask for clarification when the relationship cannot "
            "be represented safely."
        ),
    ),
    _case(
        77,
        "duplicate_catalog_name",
        "Show Fitness shops.",
        case_type="conversation",
        turns=[{"question": "Show Fitness shops."}],
        expected_outcome="clarification",
        expected_clarification=True,
        note=(
            "Fitness occurs under more than one catalog parent. Do not silently "
            "merge or choose between the distinct entries."
        ),
    ),
    _case(
        78,
        "ambiguous_catalog_match",
        "Show dining shops.",
        case_type="conversation",
        turns=[{"question": "Show dining shops."}],
        expected_outcome="clarification",
        expected_clarification=True,
        note=(
            "Dining can refer to Food & Dining or Kitchen & Dining. Do not "
            "silently infer a narrower restaurant filter."
        ),
    ),
]


MANUAL_DIAGNOSTIC_SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "exact-catalog-option-reply",
        "steps": [
            "Ask a question that produces a catalog clarification with options.",
            "Reply with the exact, full option label: Drinks & Beverages.",
        ],
        "expected": (
            "Accept the full option despite its length, continue the original "
            "question, and apply the selected subcategory filter."
        ),
        "capture": [
            "conversation ID and clarification note ID/version",
            "clarification options and selected catalog filter",
            "whether SQL runs without asking the same clarification again",
        ],
    },
    {
        "id": "restaurant-typo-selection",
        "steps": [
            "Create a choose-one clarification note with the real catalog "
            "option Restaurent.",
            "Reply to that note with the typo: restaruent.",
        ],
        "expected": (
            "Accept the typo only when it uniquely matches the real Restaurent "
            "catalog entry; otherwise show the options without guessing."
        ),
        "capture": [
            "reply-matching decision and selected catalog value",
            "whether the reply reaches SQL generation",
        ],
    },
    {
        "id": "catalog-choice-cache-isolation",
        "steps": [
            "Ask about beverages and select Drinks & Beverages.",
            "Repeat the same original question in a fresh conversation and "
            "select Snacks & Beverages.",
        ],
        "expected": (
            "Each answer uses the selected catalog row/ID and its level; the "
            "second choice must not replay the first choice's cached answer."
        ),
        "capture": [
            "selected catalog row IDs and levels for both conversations",
            "cache keys or cache-hit decisions",
            "final SQL filters and answers",
        ],
    },
]


def _validate_catalog(cases: List[Dict[str, Any]]) -> None:
    numbers = [case["number"] for case in cases]
    if numbers != list(range(73, 79)):
        raise ValueError("Catalog 5 must contain consecutive cases 73 through 78")
    for case in cases:
        if case["case_type"] not in {"sql", "conversation"}:
            raise ValueError(
                f"Case {case['number']} has unsupported case_type "
                f"{case['case_type']!r}"
            )
        if case["case_type"] == "sql" and not case["golden_sql"]:
            raise ValueError(f"Case {case['number']} has no golden_sql")
        if case["case_type"] == "conversation" and not case["turns"]:
            raise ValueError(f"Case {case['number']} has no conversation turns")


_validate_catalog(GOLDEN_BENCHMARK_CASES)
