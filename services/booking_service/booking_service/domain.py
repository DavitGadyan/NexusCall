"""Pure booking rules: slot generation in the resource's timezone and the appointment state machine.

No I/O here, so the rules are unit-tested directly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from nexus_core.errors import Conflict, Unprocessable

# ---------------------------------------------------------------------------------------------- state machine
TRANSITIONS: dict[str, frozenset[str]] = {
    "tentative": frozenset({"confirmed", "canceled"}),
    "confirmed": frozenset({"canceled", "completed", "no_show"}),
    "canceled": frozenset(),
    "completed": frozenset(),
    "no_show": frozenset(),
}
TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def require_transition(current: str, target: str) -> None:
    if current == target:
        return
    if not can_transition(current, target):
        raise Conflict(f"A {current} booking cannot become {target}")


# ---------------------------------------------------------------------------------------------- time helpers
def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise Unprocessable("Unknown timezone") from exc


def valid_timezone(name: str) -> bool:
    return name in available_timezones()


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise Unprocessable("Timestamps must include a timezone offset (ISO-8601)")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Rule:
    weekday: int
    start_time: time
    end_time: time
    slot_minutes: int
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    min_notice_minutes: int = 0


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end


def _local_window(day: date, rule: Rule, tz: ZoneInfo) -> Interval:
    start = datetime.combine(day, rule.start_time, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day, rule.end_time, tzinfo=tz).astimezone(UTC)
    return Interval(start, end)


def _days(start: datetime, end: datetime, tz: ZoneInfo) -> Iterable[date]:
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date()
    while day <= last:
        yield day
        day += timedelta(days=1)


def compute_slots(
    rules: Sequence[Rule],
    tz_name: str,
    range_start: datetime,
    range_end: datetime,
    busy: Sequence[Interval],
    *,
    now: datetime,
    limit: int = 200,
) -> list[Interval]:
    """Free slots of each rule's length inside [range_start, range_end).

    A slot is free when the slot plus its buffers overlaps no busy interval, and it starts after the rule's
    minimum notice. Windows are interpreted in the resource's timezone (DST-aware)."""
    tz = zone(tz_name)
    start_utc, end_utc = as_utc(range_start), as_utc(range_end)
    out: list[Interval] = []
    for day in _days(start_utc, end_utc, tz):
        for rule in sorted((r for r in rules if r.weekday == day.weekday()), key=lambda r: r.start_time):
            window = _local_window(day, rule, tz)
            step = timedelta(minutes=rule.slot_minutes)
            earliest = now + timedelta(minutes=rule.min_notice_minutes)
            before = timedelta(minutes=rule.buffer_before_minutes)
            after = timedelta(minutes=rule.buffer_after_minutes)
            cursor = window.start
            while cursor + step <= window.end:
                slot = Interval(cursor, cursor + step)
                padded = Interval(slot.start - before, slot.end + after)
                if (
                    slot.start >= start_utc
                    and slot.end <= end_utc
                    and slot.start >= earliest
                    and not any(padded.overlaps(b) for b in busy)
                ):
                    out.append(slot)
                    if len(out) >= limit:
                        return out
                cursor += step
    return sorted(out, key=lambda s: s.start)


def fits_rules(
    rules: Sequence[Rule], tz_name: str, start: datetime, end: datetime, *, now: datetime, exact_slot: bool
) -> bool:
    """True when [start, end) lies inside one rule window (and, with `exact_slot`, is one of its slots)."""
    tz = zone(tz_name)
    start, end = as_utc(start), as_utc(end)
    if end <= start:
        return False
    for day in _days(start, end, tz):
        for rule in rules:
            if rule.weekday != day.weekday():
                continue
            window = _local_window(day, rule, tz)
            if start < window.start or end > window.end:
                continue
            if start < now + timedelta(minutes=rule.min_notice_minutes):
                continue
            if not exact_slot:
                return True
            step = timedelta(minutes=rule.slot_minutes)
            offset = start - window.start
            if end - start == step and offset % step == timedelta(0):
                return True
    return False


def event_id_for(tenant_id: object, appointment_id: object, version: int) -> str:
    """Deterministic external event id. Google requires base32hex characters (a-v, 0-9), 5..1024 long."""
    digest = hashlib.sha256(f"{tenant_id}:{appointment_id}:{version}".encode()).hexdigest()
    return f"nc{digest[:40]}"
