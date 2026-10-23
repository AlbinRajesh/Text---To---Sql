"""Resolve structured relative-period intents into canonical date ranges."""

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Kolkata"
_UNITS = {"day", "week", "month", "quarter", "year"}
_WINDOWS = {"today", "yesterday", "previous_complete", "rolling", "current_year"}


class DateResolutionError(ValueError):
    """Raised when a structured temporal intent or its timezone is invalid."""


def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _subtract_period(value: date, unit: str, count: int) -> date:
    if unit == "day":
        return value - timedelta(days=count)
    if unit == "week":
        return value - timedelta(weeks=count)
    if unit == "month":
        return _shift_months(value, count)
    if unit == "quarter":
        return _shift_months(value, count * 3)
    if unit == "year":
        return _shift_months(value, count * 12)
    raise DateResolutionError(f"Unsupported temporal unit: {unit!r}.")


def _start_of_current_period(value: date, unit: str) -> date:
    if unit == "day":
        return value
    if unit == "week":
        return value - timedelta(days=value.weekday())
    if unit == "month":
        return value.replace(day=1)
    if unit == "quarter":
        first_month = ((value.month - 1) // 3) * 3 + 1
        return value.replace(month=first_month, day=1)
    if unit == "year":
        return value.replace(month=1, day=1)
    raise DateResolutionError(f"Unsupported temporal unit: {unit!r}.")


def _validate_intent(intent: Any) -> dict[str, Any]:
    if not isinstance(intent, dict):
        raise DateResolutionError("Each temporal intent must be an object.")

    kind = intent.get("kind")
    if kind == "relative_period":
        if set(intent) != {"kind", "target", "window", "unit", "count"}:
            raise DateResolutionError(
                "A relative-period intent must contain kind, target, window, unit, and count."
            )
        target = intent["target"]
        window = intent["window"]
        unit = intent["unit"]
        count = intent["count"]
        if not isinstance(target, str) or not target.strip():
            raise DateResolutionError("A relative-period intent needs a target.")
        if not isinstance(window, str) or window not in _WINDOWS:
            raise DateResolutionError(f"Unsupported temporal window: {window!r}.")
        if not isinstance(unit, str) or unit not in _UNITS:
            raise DateResolutionError(f"Unsupported temporal unit: {unit!r}.")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 120:
            raise DateResolutionError("Temporal count must be an integer from 1 to 120.")
        if window in {"today", "yesterday"} and (unit != "day" or count != 1):
            raise DateResolutionError(
                f"The {window} window must use unit='day' and count=1."
            )
        if window == "current_year" and (unit != "year" or count != 1):
            raise DateResolutionError(
                "The current_year window must use unit='year' and count=1."
            )
        if window in {"today", "yesterday"}:
            return {
                "kind": kind,
                "target": target.strip(),
                "window": window,
                "unit": unit,
                "count": count,
            }
        return {
            "kind": kind,
            "target": target.strip(),
            "window": window,
            "unit": unit,
            "count": count,
        }

    if kind == "current_state":
        if set(intent) != {"kind", "entity", "state", "business_rule_term"}:
            raise DateResolutionError(
                "A current-state intent must contain kind, entity, state, and business_rule_term."
            )
        if any(
            not isinstance(intent[key], str) or not intent[key].strip()
            for key in ("entity", "state", "business_rule_term")
        ):
            raise DateResolutionError(
                "A current-state intent needs non-empty entity, state, and business_rule_term values."
            )
        return {
            "kind": kind,
            "entity": intent["entity"].strip(),
            "state": intent["state"].strip(),
            "business_rule_term": intent["business_rule_term"].strip(),
        }

    raise DateResolutionError(f"Unsupported temporal intent kind: {kind!r}.")


def resolve_temporal_intents(
    intents: Any,
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Resolve structured temporal intents without inspecting question text."""
    if not isinstance(intents, list):
        raise DateResolutionError("temporal_intents must be a list.")

    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise DateResolutionError(
            f"Configured business timezone is invalid: {timezone_name!r}."
        ) from exc

    current_time = now or datetime.now(timezone)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise DateResolutionError("The resolver clock must be timezone-aware.")
    local_today = current_time.astimezone(timezone).date()

    date_ranges = []
    current_state_filters = []
    for raw_intent in intents:
        intent = _validate_intent(raw_intent)
        if intent["kind"] == "current_state":
            current_state_filters.append(intent)
            continue

        unit = intent["unit"]
        count = intent["count"]
        window = intent["window"]
        if window == "today":
            start = local_today
            end = local_today + timedelta(days=1)
        elif window == "yesterday":
            start = local_today - timedelta(days=1)
            end = local_today
        elif window == "previous_complete":
            end = _start_of_current_period(local_today, unit)
            start = _subtract_period(end, unit, count)
        elif window == "current_year":
            start = local_today.replace(month=1, day=1)
            end = start.replace(year=start.year + 1)
        else:
            start = _subtract_period(local_today, unit, count)
            end = local_today + timedelta(days=1)

        date_ranges.append({
            "target": intent["target"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": timezone_name,
            "boundary": "[start,end)",
        })

    return {
        "date_ranges": date_ranges,
        "current_state_filters": current_state_filters,
    }
