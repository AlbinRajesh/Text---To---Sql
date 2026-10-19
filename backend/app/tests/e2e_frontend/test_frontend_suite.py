from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import pytest
from playwright.sync_api import Response

from app.tests.e2e_frontend.cases import CaseSpec, case_parameters
from app.tests.e2e_frontend.cases_batch2 import (
    case_parameters_batch2,
    run_batch2_case,
)
from app.tests.e2e_frontend.cases_batch3 import (
    case_parameters_batch3,
    run_batch3_case,
)
from app.tests.e2e_frontend.cases_batch4 import (
    case_parameters_batch4,
    run_batch4_case,
)
from app.tests.e2e_frontend.pages.chat_page import ChatPage
from app.tests.e2e_frontend.utils.oracle import fetch_oracle_case
from app.tests.e2e_frontend.utils.sse_capture import (
    RateLimitError,
    parse_stream_events,
    stream_result,
    stream_rows,
)


def _capture_response(page, backend_url: str, action: Callable[[], None]) -> tuple[Response, list[dict[str, Any]]]:
    stream_url = f"{backend_url.rstrip('/')}/api/chat/stream"
    with page.expect_response(
        lambda response: response.url.split("?", maxsplit=1)[0] == stream_url,
        timeout=90_000,
    ) as response_info:
        action()

    response = response_info.value
    try:
        events = parse_stream_events(response.body(), response.status)
    except RateLimitError as exc:
        pytest.exit(str(exc), returncode=1)
    if response.status >= 400:
        pytest.fail(
            f"API_ERROR: /api/chat/stream returned HTTP {response.status}: "
            f"{response.text()[:1000]}"
        )
    return response, events


def _send(chat: ChatPage, backend_url: str, question: str):
    return _capture_response(
        chat.page,
        backend_url,
        lambda: chat.ask(question),
    )


def _click_option(chat: ChatPage, backend_url: str, option: Callable[[], None]):
    return _capture_response(chat.page, backend_url, option)


def _assert_status(case: CaseSpec, events: list[dict[str, Any]], expected: str | None = None):
    result = stream_result(events)
    assert result is not None, f"case {case.id} produced no result event"
    wanted = expected or case.expected_status
    if wanted is not None:
        assert result.get("status") == wanted, (
            f"case {case.id} expected stream status {wanted!r}, "
            f"got {result.get('status')!r}"
        )
    return result


def _canonical_key(key: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).strip().lower())
    if normalized == "shopname":
        return "name"
    return normalized


def _canonical_value(value: Any) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return ("text", str(value).strip())
    if not number.is_finite():
        return ("text", str(value).strip())
    return ("number", str(number.normalize()))


_IDENTIFIER_WORDS = {
    "code",
    "guid",
    "identifier",
    "id",
    "ids",
    "mobile",
    "phone",
    "pincode",
    "postcode",
    "uuid",
    "zipcode",
}


def _is_identifier_column(column: str) -> bool:
    parts = re.split(r"[^a-z0-9]+", str(column).lower())
    return any(part in _IDENTIFIER_WORDS for part in parts if part)


def _question_requests_identifiers(question: str) -> bool:
    return re.search(r"\b(id|identifier|code)\b", question, flags=re.IGNORECASE) is not None


def _column_mapping(case: CaseSpec) -> dict[str, str]:
    mapping = {
        _canonical_key(oracle_column): _canonical_key(actual_column)
        for oracle_column, actual_column in case.column_map.items()
    }
    return mapping


def _value_matches(expected: Any, actual: Any, match_type: str, column: str) -> bool:
    if match_type == "exact":
        return _canonical_value(expected) == _canonical_value(actual)
    if match_type == "prefix":
        expected_text = str(expected).casefold()
        actual_text = str(actual).casefold()
        if actual_text == expected_text:
            return True
        suffix = actual_text[len(expected_text):] if actual_text.startswith(expected_text) else ""
        return (
            actual_text.startswith(expected_text)
            and re.fullmatch(r" \d{4}", suffix) is not None
        )
    raise AssertionError(
        f"Unsupported value_match {match_type!r} for oracle column {column!r}; "
        "expected 'exact' or 'prefix'."
    )


def _comparison_value(
    value: Any,
    match_type: str,
    *,
    normalize_screen: bool = False,
    numeric_tolerance: bool = False,
) -> tuple[str, Any]:
    if normalize_screen:
        text = str(value).strip()
        numeric_text = text.replace(",", "")
        try:
            number = Decimal(numeric_text)
        except (InvalidOperation, ValueError):
            return ("text", text)
        if number.is_finite():
            return (
                "screen_float" if numeric_tolerance else "screen_integer",
                number,
            )
        return ("text", text)
    if match_type == "normalized_text":
        normalized = " ".join(str(value).split()).casefold()
        return ("text", normalized)
    if match_type == "month_with_optional_year":
        normalized = " ".join(str(value).split()).casefold()
        if re.fullmatch(r".+ 2025", normalized):
            normalized = normalized[:-5]
        return ("text", normalized)
    if match_type == "datetime":
        text = str(value).strip()
        try:
            normalized_datetime = datetime.fromisoformat(
                text.replace("Z", "+00:00")
            ).isoformat()
        except ValueError as exc:
            raise AssertionError(
                f"Expected a datetime-comparable value, got {value!r}"
            ) from exc
        return ("datetime", normalized_datetime)
    if match_type == "integer":
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise AssertionError(f"Expected an integer-comparable value, got {value!r}") from exc
        if not number.is_finite() or number != number.to_integral_value():
            raise AssertionError(f"Expected an integer-comparable value, got {value!r}")
        return ("integer", str(int(number)))
    if match_type == "prefix":
        text = str(value)
        if re.search(r" \d{4}$", text):
            text = text[:-5]
        return ("text", text.casefold())
    return _canonical_value(value)


def _row_columns(rows: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(column) for row in rows for column in row))


def _record_alias_keys(case: CaseSpec, actual: list[dict[str, Any]]) -> None:
    if not case.compare_by_position:
        return

    raw_keys = list(actual[0].keys()) if actual else []
    report_path = (
        Path(__file__).resolve().parent
        / "reports"
        / f"alias_keys_{case.id.lower()}.jsonl"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)

    prior_runs = []
    if report_path.exists():
        prior_runs = [
            json.loads(line)
            for line in report_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    run_entry = {
        "case_id": case.id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stream_keys": raw_keys,
    }
    with report_path.open("a", encoding="utf-8") as report_file:
        report_file.write(json.dumps(run_entry, ensure_ascii=False) + "\n")

    previous_key_sets = {
        tuple(run["stream_keys"])
        for run in prior_runs
    }
    if previous_key_sets and any(keys != tuple(raw_keys) for keys in previous_key_sets):
        print(
            "finding=ALIAS_UNSTABLE "
            f"case={case.id} report={report_path} "
            f"previous_stream_keys={sorted(previous_key_sets)!r} "
            f"current_stream_keys={raw_keys!r}"
        )


def _compare_oracle_rows(
    case: CaseSpec,
    oracle_rows: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    source: str,
    *,
    question: str | None = None,
) -> None:
    assert case.oracle_id is not None
    mapping = _column_mapping(case)
    ignored = {_canonical_key(column) for column in case.ignore_columns}
    value_matches = {
        _canonical_key(column): match_type
        for column, match_type in case.value_match.items()
    }
    oracle_columns = list(oracle_rows[0]) if oracle_rows else []
    oracle_column_names = _row_columns(oracle_rows)
    actual_columns = _row_columns(actual)

    if source == "stream":
        _record_alias_keys(case, actual)

    hidden_columns = [
        column for column in oracle_columns
        if _is_identifier_column(column)
    ]
    if (
        source == "screen"
        and not _question_requests_identifiers(question or case.question)
        and len(hidden_columns) < len(oracle_columns)
    ):
        # The UI hides identifier columns by design unless the question asks for them.
        ignored.update(_canonical_key(column) for column in hidden_columns)

    compared_columns = [
        column for column in oracle_columns
        if _canonical_key(column) not in ignored
    ]

    def fail(message: str) -> None:
        print(f"case {case.id} {source} comparison failed: {message}")
        print(f"oracle columns: {oracle_column_names}")
        print(f"actual columns: {actual_columns}")
        oracle_name_column = next(
            (key for key in oracle_columns if _canonical_key(key) == "name"),
            None,
        )
        actual_name_column = mapping.get("name", "name")
        if oracle_name_column and any(
            _canonical_key(key) == actual_name_column for key in actual_columns
        ):
            actual_name_column = next(
                key for key in actual_columns
                if _canonical_key(key) == actual_name_column
            )
            oracle_names = {
                str(row[oracle_name_column]) for row in oracle_rows
            }
            actual_names = {
                str(row[actual_name_column])
                for row in actual
                if actual_name_column in row
            }
            print(
                f"oracle row count: {len(oracle_rows)}; "
                f"{source} row count: {len(actual)}"
            )
            print(f"name sets equal: {oracle_names == actual_names}")
            print(
                "names only in oracle (up to 5): "
                f"{sorted(oracle_names - actual_names)[:5]!r}"
            )
            print(
                "names only in actual (up to 5): "
                f"{sorted(actual_names - oracle_names)[:5]!r}"
            )
        raise AssertionError(message)

    invalid_value_matches = {
        column: match_type
        for column, match_type in case.value_match.items()
        if match_type not in {
            "exact",
            "prefix",
            "normalized_text",
            "integer",
            "month_with_optional_year",
            "datetime",
        }
    }
    if invalid_value_matches:
        fail(
            "unsupported value_match rule(s): "
            f"{invalid_value_matches!r}; expected 'exact' or 'prefix'"
        )

    if len(oracle_rows) != len(actual):
        fail(
            f"row count differs: oracle has {len(oracle_rows)}, "
            f"{source} has {len(actual)}"
        )

    if case.compare_by_position and len(actual_columns) != len(compared_columns):
        fail(
            f"column count differs: oracle has {len(compared_columns)} columns "
            f"{oracle_column_names!r}, {source} has {len(actual_columns)} columns "
            f"{actual_columns!r}"
        )

    expected_by_row: list[dict[str, Any]] = []
    actual_by_row: list[dict[str, Any]] = []
    for row_index, row in enumerate(oracle_rows):
        expected_by_row.append({_canonical_key(key): value for key, value in row.items()})
    for row_index, row in enumerate(actual):
        actual_by_row.append({_canonical_key(key): value for key, value in row.items()})

    def normalized_rows(
        rows: list[dict[str, Any]],
        *,
        is_oracle: bool,
    ) -> list[tuple[Any, ...]]:
        signatures = []
        for row_index, row in enumerate(rows):
            signature = []
            if case.compare_by_position and not is_oracle:
                if len(row) != len(compared_columns):
                    fail(
                        f"{source} row {row_index} has {len(row)} values for "
                        f"{len(compared_columns)} compared oracle columns; "
                        f"available columns: {list(row)!r}"
                    )
                actual_values = list(row.values())
                for column_index, oracle_column in enumerate(compared_columns):
                    match_type = value_matches.get(
                        _canonical_key(oracle_column),
                        "exact",
                    )
                    canonical_column = _canonical_key(oracle_column)
                    sample_value = oracle_rows[0].get(oracle_column)
                    numeric_value = (
                        not _is_identifier_column(oracle_column)
                        and isinstance(sample_value, (int, float, Decimal))
                        and not isinstance(sample_value, bool)
                    )
                    numeric_tolerance = (
                        numeric_value
                        and isinstance(sample_value, (float, Decimal))
                        and Decimal(str(sample_value))
                        != Decimal(str(sample_value)).to_integral_value()
                    )
                    signature.append(
                        (
                            canonical_column,
                            _comparison_value(
                                actual_values[column_index],
                                match_type,
                                normalize_screen=(
                                    source == "screen"
                                    and case.normalize_screen_values
                                    and numeric_value
                                ),
                                numeric_tolerance=numeric_tolerance,
                            ),
                        )
                    )
                signatures.append(tuple(signature))
                continue

            for oracle_column in compared_columns:
                canonical_oracle_column = _canonical_key(oracle_column)
                mapped_column = (
                    canonical_oracle_column
                    if is_oracle
                    else mapping.get(canonical_oracle_column, canonical_oracle_column)
                )
                if mapped_column not in row:
                    missing_name = (
                        oracle_column
                        if is_oracle
                        else mapping.get(canonical_oracle_column, oracle_column)
                    )
                    fail(
                        f"{'oracle' if is_oracle else source} row {row_index} "
                        f"is missing required column {missing_name!r}; "
                        f"available columns: {list(row)!r}"
                    )
                match_type = value_matches.get(canonical_oracle_column, "exact")
                sample_value = oracle_rows[0].get(oracle_column)
                numeric_value = (
                    not _is_identifier_column(oracle_column)
                    and isinstance(sample_value, (int, float, Decimal))
                    and not isinstance(sample_value, bool)
                )
                numeric_tolerance = (
                    numeric_value
                    and isinstance(sample_value, (float, Decimal))
                    and Decimal(str(sample_value))
                    != Decimal(str(sample_value)).to_integral_value()
                )
                signature.append(
                    (
                        canonical_oracle_column,
                        _comparison_value(
                            row[mapped_column],
                            match_type,
                            normalize_screen=(
                                source == "screen"
                                and case.normalize_screen_values
                                and numeric_value
                            ),
                            numeric_tolerance=numeric_tolerance,
                        ),
                    )
                )
            signatures.append(tuple(signature))
        return signatures

    normalized_oracle_rows = normalized_rows(expected_by_row, is_oracle=True)
    normalized_actual_rows = normalized_rows(actual_by_row, is_oracle=False)
    for oracle_column in compared_columns:
        canonical_oracle_column = _canonical_key(oracle_column)
        match_type = value_matches.get(canonical_oracle_column, "exact")
        if match_type not in {"prefix", "month_with_optional_year"}:
            continue
        if case.compare_by_position:
            column_index = compared_columns.index(oracle_column)
            actual_values = [
                list(row.values())[column_index]
                for row in actual_by_row
            ]
        else:
            mapped_column = mapping.get(canonical_oracle_column, canonical_oracle_column)
            actual_values = [row[mapped_column] for row in actual_by_row]
        oracle_values = [
            row[canonical_oracle_column]
            for row in expected_by_row
        ]
        for row_index, actual_value in enumerate(actual_values):
            if match_type == "month_with_optional_year":
                normalized_actual = " ".join(str(actual_value).split()).casefold()
                valid_values = {
                    " ".join(str(expected_value).split()).casefold()
                    for expected_value in oracle_values
                }
                is_valid = normalized_actual in valid_values or any(
                    re.fullmatch(
                        rf"{re.escape(expected_value)} 2025",
                        normalized_actual,
                    )
                    is not None
                    for expected_value in valid_values
                )
            else:
                is_valid = any(
                    _value_matches(expected_value, actual_value, "prefix", oracle_column)
                    for expected_value in oracle_values
                )
            if not is_valid:
                fail(
                    f"{source} row {row_index}, column {oracle_column!r} "
                    f"does not match its configured value rule "
                    f"{match_type!r}: {actual_value!r}; oracle values: {oracle_values!r}"
                )

    def signatures_match(
        expected_signature: tuple[Any, ...],
        actual_signature: tuple[Any, ...],
    ) -> bool:
        if len(expected_signature) != len(actual_signature):
            return False
        for expected_cell, actual_cell in zip(
            expected_signature,
            actual_signature,
        ):
            if expected_cell[0] != actual_cell[0]:
                return False
            expected_value = expected_cell[1]
            actual_value = actual_cell[1]
            if (
                expected_value[0] == actual_value[0] == "screen_float"
                and source == "screen"
                and case.normalize_screen_values
            ):
                if abs(expected_value[1] - actual_value[1]) > Decimal("0.005"):
                    return False
            elif expected_value != actual_value:
                return False
        return True

    signatures_equal = Counter(normalized_oracle_rows) == Counter(
        normalized_actual_rows
    )
    if not signatures_equal and source == "screen" and case.normalize_screen_values:
        unmatched = list(normalized_actual_rows)
        signatures_equal = True
        for expected_signature in normalized_oracle_rows:
            match_index = next(
                (
                    index
                    for index, actual_signature in enumerate(unmatched)
                    if signatures_match(expected_signature, actual_signature)
                ),
                None,
            )
            if match_index is None:
                signatures_equal = False
                break
            unmatched.pop(match_index)

    if not signatures_equal:
        fail(
            f"{source} row values differ; oracle={oracle_rows!r}, "
            f"{source}={actual!r}"
        )


_BATCH3_COUNT_COLUMNS = {
    "shopcount",
    "shopswithoffers",
    "activeshopswithoutbookings",
    "reviewercount",
    "bookingcount",
    "completedbookingcount",
    "categorycount",
    "uniquecitycount",
    "activeoffercount",
    "reviewcount",
    "subcategorycount",
}
_BATCH3_COLUMN_ALIASES = {
    "categoryname": {"categoriesname"},
    "subcategoryname": {"subcategory"},
    "reviewername": {"name"},
    "reviewdate": {"createdat"},
    "bookingmonth": {"timeslotmonth", "month"},
    "averagefinalamount": {"avgfinalamount"},
    "averagerating": {"avgrating", "ratingaverage"},
    "totalrevenue": {"revenue", "bookingrevenue", "completedbookingrevenue"},
    "completedbookingrevenue": {"totalrevenue", "revenue", "bookingrevenue"},
}


def _compare_batch3_oracle_rows(
    case: CaseSpec,
    oracle_rows: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    source: str,
) -> None:
    if not oracle_rows or not actual:
        _compare_oracle_rows(case, oracle_rows, actual, source)
        return

    mapping = dict(case.column_map)
    actual_columns = _row_columns(actual)
    actual_keys = {_canonical_key(column) for column in actual_columns}
    used_actual_keys = set(mapping.values())
    count_keys = {
        _canonical_key(column)
        for column in actual_columns
        if "count" in _canonical_key(column)
    }

    for oracle_column in _row_columns(oracle_rows):
        oracle_key = _canonical_key(oracle_column)
        if oracle_key in actual_keys or oracle_key in mapping:
            continue

        candidates = _BATCH3_COLUMN_ALIASES.get(oracle_key, set()) & actual_keys
        if oracle_key in _BATCH3_COUNT_COLUMNS:
            candidates = set(count_keys)
        candidates -= used_actual_keys
        if len(candidates) == 1:
            actual_key = candidates.pop()
            mapping[oracle_key] = actual_key
            used_actual_keys.add(actual_key)
            continue

        if oracle_key in _BATCH3_COUNT_COLUMNS and not candidates:
            numeric_columns = set()
            for column in actual_columns:
                key = _canonical_key(column)
                if key in used_actual_keys or any(
                    identifier in key
                    for identifier in ("id", "identifier", "code", "phone")
                ):
                    continue
                try:
                    numbers = [
                        Decimal(str(row[column]).strip().replace(",", ""))
                        for row in actual
                    ]
                except (InvalidOperation, ValueError, KeyError):
                    continue
                if all(number.is_finite() for number in numbers):
                    numeric_columns.add(key)
            if len(numeric_columns) == 1:
                actual_key = numeric_columns.pop()
                mapping[oracle_key] = actual_key
                used_actual_keys.add(actual_key)

    _compare_oracle_rows(
        replace(case, column_map=mapping),
        oracle_rows,
        actual,
        source,
    )


def _assert_oracle_table(
    case: CaseSpec,
    chat: ChatPage,
    events: list[dict[str, Any]],
    *,
    require_expand: bool = False,
    screen_question: str | None = None,
) -> list[dict[str, Any]]:
    assert case.oracle_id is not None
    result = _assert_status(case, events, "ok")
    chat.wait_for_table()

    expanded = chat.expand_all_rows()
    if require_expand:
        assert expanded is not None, f"case {case.id} expected a 'Show all rows' control"

    oracle_rows = fetch_oracle_case(case.oracle_id)
    response_rows = list(result.get("results") or [])
    visible_rows = chat.table_rows()
    _compare_oracle_rows(case, oracle_rows, response_rows, "stream")
    _compare_oracle_rows(
        case,
        oracle_rows,
        visible_rows,
        "screen",
        question=screen_question,
    )
    return oracle_rows


def _ask_and_click_search_option(
    case: CaseSpec,
    chat: ChatPage,
    backend_url: str,
    term: str,
) -> list[dict[str, Any]]:
    _send(chat, backend_url, case.question)
    response, events = _click_option(
        chat,
        backend_url,
        lambda: chat.click_search_option(term),
    )
    assert response.status == 200
    return events


def _run_case(case: CaseSpec, chat: ChatPage, backend_url: str) -> None:
    if case.scenario.startswith("batch4_"):
        run_batch4_case(
            case,
            chat,
            backend_url,
            send=_send,
            assert_status=_assert_status,
            stream_rows=stream_rows,
            compare_rows=_compare_batch3_oracle_rows,
        )
        return

    if case.scenario.startswith("batch3_"):
        run_batch3_case(
            case,
            chat,
            backend_url,
            send=_send,
            assert_status=_assert_status,
            stream_rows=stream_rows,
            compare_rows=_compare_batch3_oracle_rows,
        )
        return

    if case.scenario.startswith("batch2_"):
        run_batch2_case(
            case,
            chat,
            backend_url,
            send=_send,
            assert_status=_assert_status,
            stream_rows=stream_rows,
            fetch_existing=fetch_oracle_case,
        )
        return

    if case.scenario in {
        "oracle_table",
        "expandable_oracle_table",
        "decimal_oracle_table",
    }:
        _, events = _send(chat, backend_url, case.question)
        _assert_oracle_table(
            case,
            chat,
            events,
            require_expand=case.scenario == "expandable_oracle_table",
        )
        if case.scenario == "decimal_oracle_table":
            for row in chat.table_rows():
                rating = next(
                    (
                        value
                        for key, value in row.items()
                        if _canonical_key(key) == "averagerating"
                    ),
                    None,
                )
                assert rating is not None
                assert re.search(r"\.\d{2}$", rating), (
                    f"case {case.id} average rating is not displayed to two decimals: {rating!r}"
                )
        return

    if case.scenario == "oracle_count":
        _, events = _send(chat, backend_url, case.question)
        result = _assert_status(case, events, "ok")
        oracle_rows = fetch_oracle_case(case.oracle_id)
        _compare_oracle_rows(case, oracle_rows, stream_rows(events), "stream")
        assert len(oracle_rows) == 1 and len(oracle_rows[0]) == 1
        count = next(iter(oracle_rows[0].values()))
        page_text = chat.wait_for_visible_text(str(count))
        assert not chat.has_table(), "case 03 should render the count as an answer, not a table"
        assert str(count) in page_text
        assert len(result.get("results") or []) == 1
        return

    if case.scenario == "juice_search":
        events = _ask_and_click_search_option(case, chat, backend_url, "juice")
        assert case.expected_message_contains
        chat.expect_message_contains(case.expected_message_contains)
        _assert_oracle_table(case, chat, events)
        return

    if case.scenario == "cached_juice_search":
        first_events = _ask_and_click_search_option(case, chat, backend_url, "juice")
        _assert_oracle_table(case, chat, first_events)
        chat.start_new_chat()
        second_events = _ask_and_click_search_option(case, chat, backend_url, "juice")
        assert case.expected_message_contains
        chat.expect_message_contains(case.expected_message_contains)
        chat.expect_message_contains("Instant answer from cache")
        _assert_oracle_table(case, chat, second_events)
        return

    if case.scenario == "bakery_search":
        response, events = _click_option(
            chat,
            backend_url,
            lambda: chat.ask(case.question),
        )
        assert response.status == 200
        response, events = _click_option(
            chat,
            backend_url,
            lambda: chat.click_search_option("bakery"),
        )
        assert response.status == 200
        assert case.expected_message_contains
        chat.expect_message_contains(case.expected_message_contains)
        result = _assert_status(case, events, "ok")
        oracle_rows = fetch_oracle_case(case.oracle_id)
        assert oracle_rows == []
        assert stream_rows(events) == []
        assert not chat.has_table(), "case 11 must not render an empty table"
        assert result.get("row_count", 0) == 0
        return

    if case.scenario == "csv_download":
        _, events = _send(chat, backend_url, case.question)
        oracle_rows = _assert_oracle_table(case, chat, events, require_expand=True)
        download = chat.download_csv()
        path = download.path()
        assert path is not None
        with Path(path).open("r", encoding="utf-8-sig", newline="") as csv_file:
            csv_rows = [dict(row) for row in csv.DictReader(csv_file)]
        _compare_oracle_rows(case, oracle_rows, csv_rows, "downloaded CSV")
        return

    if case.scenario == "follow_up":
        _, first_events = _send(chat, backend_url, case.question)
        _assert_status(case, first_events, "ok")
        follow_up = (
            "Which shops contributed most to that total? Show only the top five, "
            "ranked by completed booking revenue."
        )
        _, second_events = _send(chat, backend_url, follow_up)
        _assert_oracle_table(case, chat, second_events)
        return

    if case.scenario == "sql_confirmation":
        request_urls: list[str] = []
        chat.page.on(
            "request",
            lambda request: request_urls.append(request.url)
            if request.url.split("?", maxsplit=1)[0].endswith("/api/chat/stream")
            else None,
        )
        _, confirmation_events = _send(chat, backend_url, case.question)
        _assert_status(case, confirmation_events, "confirmation")
        assert case.expected_message_contains
        chat.expect_message_contains(case.expected_message_contains)

        chat.fill_question("yes")
        _, execution_events = _capture_response(
            chat.page,
            backend_url,
            chat.send_twice,
        )
        _assert_oracle_table(
            case,
            chat,
            execution_events,
            screen_question="yes",
        )
        assert len(request_urls) == 2, (
            "case 07 should send one confirmation request and exactly one execution "
            f"request; observed {len(request_urls)} stream requests"
        )
        return

    if case.scenario == "approved_message":
        _, events = _send(chat, backend_url, case.question)
        if case.id == "D9":
            try:
                result = _assert_status(case, events)
                assert case.expected_message_contains
                page_text = chat.wait_for_visible_text(case.expected_message_contains)
                assert chat.has_reference_id(), "D9 should display its approved Reference ID"
                assert not chat.has_table(), "case D9 should not render a data table"
                assert case.expected_message_contains.lower() in page_text.lower()
            finally:
                chat.save_evidence(case.id)
            return

        result = _assert_status(case, events)
        assert case.expected_message_contains
        page_text = chat.wait_for_visible_text(case.expected_message_contains)
        if case.expected_status != "ok":
            assert not result.get("results"), f"case {case.id} unexpectedly returned data rows"
            assert not chat.has_table(), f"case {case.id} should not render a data table"
        assert case.expected_message_contains.lower() in page_text.lower()
        return

    if case.scenario == "or_and_unknown":
        _, or_events = _send(chat, backend_url, case.question)
        _assert_status(case, or_events, "ok")
        assert case.expected_message_contains
        chat.expect_message_contains(case.expected_message_contains)

        and_question = "Show shops that sell drinks and spaceships."
        _, and_events = _send(chat, backend_url, and_question)
        result = _assert_status(case, and_events, "clarification")
        expected_and_text = (
            "The category catalog does not verify which shops sell 'spaceships'"
        )
        chat.expect_message_contains(expected_and_text)
        assert not result.get("results")
        return

    if case.scenario == "replace_pending_clarification":
        try:
            _, clarification_events = _send(chat, backend_url, case.question)
            _assert_status(case, clarification_events, "clarification")
            _, answer_events = _send(chat, backend_url, "Show Fitness shops.")
            _assert_status(case, answer_events, "ok")
            chat.expect_message_contains("2 rows")
            option_label = "Shops in either one (or)"
            stale_option_present = chat.has_option(option_label)
            if stale_option_present:
                chat.save_evidence(case.id, option_label=option_label)
                assert chat.option_is_disabled(option_label), (
                    "case 16 left the old clarification option enabled"
                )
                stream_url = f"{backend_url.rstrip('/')}/api/chat/stream"
                stream_requests: list[str] = []
                chat.page.on(
                    "request",
                    lambda request: stream_requests.append(request.url)
                    if request.url.split("?", maxsplit=1)[0] == stream_url
                    else None,
                )
                chat.page.get_by_role(
                    "button", name=option_label, exact=True
                ).click(force=True)
                chat.page.wait_for_timeout(500)
                assert not stream_requests, (
                    "case 16 sent a /api/chat/stream request after force-clicking "
                    "a disabled stale clarification option"
                )
            else:
                raise AssertionError(
                    "case 16 expected the previous clarification option to remain visible"
                )
        finally:
            chat.save_evidence(
                case.id,
                option_label="Shops in either one (or)",
            )
        return

    if case.scenario == "refresh_pending_clarification":
        _, clarification_events = _send(chat, backend_url, case.question)
        _assert_status(case, clarification_events, "clarification")
        chat.page.reload(wait_until="domcontentloaded")
        _, answer_events = _send(chat, backend_url, "Show Fitness shops.")
        _assert_status(case, answer_events, "ok")
        chat.expect_message_contains("2 rows")
        chat.fill_question("check composer state")
        assert chat.send_is_enabled(), (
            "case 17 Send stayed disabled after entering non-empty valid text"
        )
        assert not chat.has_loading_state(), (
            "case 17 showed a stale pending/loading state after refresh"
        )
        return

    if case.scenario == "invalid_input":
        request_urls: list[str] = []
        chat.page.on(
            "request",
            lambda request: request_urls.append(request.url)
            if request.url.split("?", maxsplit=1)[0].endswith("/api/chat/stream")
            else None,
        )
        chat.fill_question("   ")
        assert not chat.send_is_enabled(), "Whitespace-only input should not be sendable."
        chat.fill_question("x" * 1001)
        assert not chat.send_is_enabled(), "Input over 1000 characters should not be sendable."
        assert request_urls == [], f"Invalid input unexpectedly sent requests: {request_urls!r}"
        return

    if case.scenario == "hostile_input":
        page_errors: list[str] = []
        chat.page.on("pageerror", lambda error: page_errors.append(str(error)))
        _, events = _send(chat, backend_url, case.question)
        assert stream_result(events) is not None, (
            f"case {case.id} returned no final stream result"
        )
        assert chat.question_input.is_visible(), f"case {case.id} left the UI unusable"
        assert chat.page.evaluate("document.readyState") == "complete"
        assert chat.page.evaluate("window.__e2eXss === true") is False, (
            f"case {case.id} executed a script from user-provided text"
        )
        assert page_errors == [], f"case {case.id} caused browser errors: {page_errors!r}"
        return

    raise AssertionError(f"Unknown e2e scenario {case.scenario!r} for {case.id}")


@pytest.mark.e2e
@pytest.mark.parametrize(
    "case",
    case_parameters()
    + case_parameters_batch2()
    + case_parameters_batch3()
    + case_parameters_batch4(),
)
def test_frontend_case(case: CaseSpec, frontend_url, backend_url, page, backend_health):
    chat = ChatPage(page, frontend_url)
    _run_case(case, chat, backend_url)
