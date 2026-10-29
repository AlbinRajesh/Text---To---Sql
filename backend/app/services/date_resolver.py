"""Resolve structured relative-period intents into canonical date ranges."""

from calendar import monthrange
from datetime import date, datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Kolkata"
_UNITS = {"day", "week", "month", "quarter", "year"}
_WINDOWS = {
    "today", "yesterday", "tomorrow", "previous_complete", "rolling",
    "current_year", "current_period", "current_complete",
}
_TEMPORAL_PHRASE = re.compile(
    r"\b(?P<today>today|yesterday|tomorrow)\b"
    r"|\b(?P<this_year>this\s+year)\b"
    r"|\b(?P<this_period>this\s+(?:day|week|month|quarter))\b"
    r"|\b(?P<period>last|previous|past)\s+"
    r"(?:(?P<count>\d{1,3})\s+)?"
    r"(?P<unit>days?|weeks?|months?|quarters?|years?)\b",
    re.IGNORECASE,
)
_TARGET_ALIASES = (
    (("invoices", "invoice"), "invoices"),
    (("bookings", "booking", "reservations", "reservation"), "bookings"),
    (("reviews", "review"), "reviews"),
    (("users", "user", "customers", "customer"), "users"),
    (("subscriptions", "subscription"), "users"),
    (("offers", "offer", "deals", "deal"), "offer"),
    (("shops", "shop", "businesses", "business"), "shop_details"),
)


class DateResolutionError(ValueError):
    """Raised when a structured temporal intent or its timezone is invalid."""


def extract_relative_period_intents(question: str) -> list[dict[str, Any]]:
    """Extract supported relative calendar phrases without asking an LLM to date them."""
    if not isinstance(question, str):
        raise DateResolutionError("The temporal source question must be text.")
    intents = []
    for match in _TEMPORAL_PHRASE.finditer(question):
        context = question[max(0, match.start() - 80):match.start()].casefold()
        target = "*"
        target_position = -1
        for aliases, table in _TARGET_ALIASES:
            for alias in aliases:
                found = list(re.finditer(rf"\b{re.escape(alias)}\b", context))
                if found and found[-1].start() > target_position:
                    target = table
                    target_position = found[-1].start()
        if match.group("today"):
            window, unit, count = match.group("today").casefold(), "day", 1
        elif match.group("this_year"):
            window, unit, count = "current_year", "year", 1
        elif match.group("this_period"):
            unit = match.group("this_period").split()[-1].casefold()
            context = question[max(0, match.start() - 48):match.start()]
            window = (
                "current_complete"
                if re.search(
                    r"\b(?:end(?:s|ing)?|expir(?:e|es|ing))\s*$",
                    context,
                    re.IGNORECASE,
                )
                else "current_period"
            )
            count = 1
        else:
            unit = match.group("unit").casefold().rstrip("s")
            count = int(match.group("count") or 1)
            window = (
                "rolling"
                if match.group("period").casefold() == "past"
                or match.group("count")
                else "previous_complete"
            )
        intents.append({
            "kind": "relative_period",
            "target": target,
            "window": window,
            "unit": unit,
            "count": count,
        })
    return intents


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


def _start_of_next_period(value: date, unit: str) -> date:
    if unit == "day":
        return value + timedelta(days=1)
    if unit == "week":
        return value + timedelta(weeks=1)
    if unit == "month":
        return _shift_months(value, -1)
    if unit == "quarter":
        return _shift_months(value, -3)
    if unit == "year":
        return value.replace(year=value.year + 1)
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
        if window in {"today", "yesterday", "tomorrow"} and (
            unit != "day" or count != 1
        ):
            raise DateResolutionError(
                f"The {window} window must use unit='day' and count=1."
            )
        if window == "current_year" and (unit != "year" or count != 1):
            raise DateResolutionError(
                "The current_year window must use unit='year' and count=1."
            )
        if window in {"current_period", "current_complete"} and count != 1:
            raise DateResolutionError(
                f"The {window} window must use count=1."
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
    """Resolve wall-clock intents; empty input leaves data anchors to SQL compilation."""
    if not isinstance(intents, list):
        raise DateResolutionError("temporal_intents must be a list.")
    if not intents:
        return {"date_ranges": [], "current_state_filters": []}

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
        elif window == "tomorrow":
            start = local_today + timedelta(days=1)
            end = start + timedelta(days=1)
        elif window == "previous_complete":
            end = _start_of_current_period(local_today, unit)
            start = _subtract_period(end, unit, count)
        elif window == "current_year":
            start = local_today.replace(month=1, day=1)
            end = start.replace(year=start.year + 1)
        elif window == "current_complete":
            start = _start_of_current_period(local_today, unit)
            end = _start_of_next_period(start, unit)
        elif window == "current_period":
            start = _start_of_current_period(local_today, unit)
            end = local_today + timedelta(days=1)
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
