"""Pre-dial gates (build.md §3.1G, §11). Pure functions where possible so they are unit-testable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Verdict = Literal["proceed", "reschedule", "cancel", "fail"]


@dataclass(frozen=True, slots=True)
class GateResult:
    verdict: Verdict
    reason: str | None = None
    retry_at: datetime | None = None


PROCEED = GateResult("proceed")


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _hhmm(value: str) -> tuple[int, int]:
    hours, minutes = value.split(":", 1)
    return int(hours), int(minutes)


def in_window(window: dict[str, Any], now: datetime, tz: ZoneInfo) -> bool:
    local = now.astimezone(tz)
    days = window.get("days") or list(DAYS)
    if DAYS[local.weekday()] not in days:
        return False
    current = (local.hour, local.minute)
    return _hhmm(window.get("start", "00:00")) <= current < _hhmm(window.get("end", "23:59"))


def next_window_start(window: dict[str, Any], now: datetime, tz: ZoneInfo) -> datetime:
    """The next moment (UTC) the window opens, evaluated in the recipient's timezone."""
    local = now.astimezone(tz)
    days = window.get("days") or list(DAYS)
    start_h, start_m = _hhmm(window.get("start", "00:00"))
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        candidate = datetime(day.year, day.month, day.day, start_h, start_m, tzinfo=tz)
        if candidate > local and DAYS[candidate.weekday()] in days:
            return candidate.astimezone(UTC)
    return (now + timedelta(days=1)).astimezone(UTC)


def calling_window_gate(window: dict[str, Any] | None, now: datetime, timezone_name: str | None) -> GateResult:
    if not window:
        return PROCEED
    tz = zone(timezone_name)
    if in_window(window, now, tz):
        return PROCEED
    return GateResult("reschedule", "outside_calling_window", next_window_start(window, now, tz))


def country_of(e164: str) -> str | None:
    try:
        number = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(number):
        return None
    region = phonenumbers.region_code_for_number(number)
    if region is None:
        # Possible but unassigned ranges (e.g. the reserved +1 555 01xx demo range): attribute the number to the
        # main region of its country calling code, so the allowlist still applies to it.
        main = phonenumbers.region_code_for_country_code(number.country_code)
        region = None if main in ("ZZ", "001") else main
    return region


def country_gate(e164: str | None, allowed: list[str] | None) -> GateResult:
    if not e164:
        return GateResult("cancel", "no_phone_number")
    country = country_of(e164)
    if country is None:
        return GateResult("fail", "invalid_phone_number")
    if country not in set(allowed or []):
        return GateResult("cancel", "country_not_allowed")
    return PROCEED


def next_utc_midnight(now: datetime) -> datetime:
    tomorrow = (now.astimezone(UTC) + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


def is_e164(value: str | None) -> bool:
    if not value or not value.startswith("+") or not value[1:].isdigit() or not 8 <= len(value) <= 16:
        return False
    return country_of(value) is not None
