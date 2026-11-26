"""Unit tests for slot generation, rule fitting and the appointment state machine (no I/O)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from booking_service.domain import (
    TRANSITIONS,
    Interval,
    Rule,
    can_transition,
    compute_slots,
    event_id_for,
    fits_rules,
    require_transition,
)
from booking_service.providers.calendar import event_id_from_key
from nexus_core.errors import Conflict, Unprocessable

MONDAY = datetime(2026, 11, 30, tzinfo=UTC)  # a Monday
NOW = MONDAY - timedelta(days=7)
WEEKDAY_RULE = Rule(weekday=0, start_time=time(9), end_time=time(12), slot_minutes=60)


def test_slots_follow_rules_and_subtract_busy():
    busy = [Interval(MONDAY.replace(hour=10), MONDAY.replace(hour=11))]
    slots = compute_slots([WEEKDAY_RULE], "UTC", MONDAY, MONDAY + timedelta(days=1), busy, now=NOW)
    assert [(s.start.hour, s.end.hour) for s in slots] == [(9, 10), (11, 12)]


def test_slots_respect_buffers_and_min_notice():
    rule = Rule(0, time(9), time(12), 60, buffer_before_minutes=0, buffer_after_minutes=15, min_notice_minutes=0)
    busy = [Interval(MONDAY.replace(hour=10, minute=5), MONDAY.replace(hour=10, minute=30))]
    slots = compute_slots([rule], "UTC", MONDAY, MONDAY + timedelta(days=1), busy, now=NOW)
    # 09:00 slot + 15 min buffer reaches 10:15 and collides; 10:00 overlaps; 11:00 is free.
    assert [s.start.hour for s in slots] == [11]
    noticed = Rule(0, time(9), time(12), 60, min_notice_minutes=120)
    soon = compute_slots([noticed], "UTC", MONDAY, MONDAY + timedelta(days=1), [], now=MONDAY.replace(hour=8))
    assert [s.start.hour for s in soon] == [10, 11]


def test_slots_are_computed_in_the_resource_timezone_across_dst():
    # US DST ends on 2026-11-01. 09:00 New York is 14:00 UTC afterwards and 13:00 UTC before.
    rule_sun = Rule(weekday=6, start_time=time(9), end_time=time(10), slot_minutes=60)
    before = datetime(2026, 10, 25, tzinfo=UTC)
    after = datetime(2026, 11, 1, tzinfo=UTC)
    s1 = compute_slots([rule_sun], "America/New_York", before, before + timedelta(days=1), [], now=NOW - timedelta(60))
    s2 = compute_slots([rule_sun], "America/New_York", after, after + timedelta(days=1), [], now=NOW - timedelta(60))
    assert s1[0].start.hour == 13 and s2[0].start.hour == 14


def test_slots_are_clipped_to_the_requested_range_and_limit():
    rule = Rule(0, time(0), time(23, 30), 30)
    slots = compute_slots([rule], "UTC", MONDAY.replace(hour=9), MONDAY.replace(hour=10), [], now=NOW)
    assert [s.start.minute for s in slots] == [0, 30]
    assert len(compute_slots([rule], "UTC", MONDAY, MONDAY + timedelta(days=1), [], now=NOW, limit=5)) == 5


def test_fits_rules_exact_and_window():
    start = MONDAY.replace(hour=10)
    assert fits_rules([WEEKDAY_RULE], "UTC", start, start + timedelta(hours=1), now=NOW, exact_slot=True)
    off_grid = MONDAY.replace(hour=10, minute=15)
    assert not fits_rules([WEEKDAY_RULE], "UTC", off_grid, off_grid + timedelta(hours=1), now=NOW, exact_slot=True)
    assert fits_rules([WEEKDAY_RULE], "UTC", off_grid, off_grid + timedelta(minutes=30), now=NOW, exact_slot=False)
    late = MONDAY.replace(hour=11, minute=30)
    assert not fits_rules([WEEKDAY_RULE], "UTC", late, late + timedelta(hours=1), now=NOW, exact_slot=False)
    tuesday = start + timedelta(days=1)
    assert not fits_rules([WEEKDAY_RULE], "UTC", tuesday, tuesday + timedelta(hours=1), now=NOW, exact_slot=False)


def test_naive_timestamps_are_rejected():
    with pytest.raises(Unprocessable):
        compute_slots([WEEKDAY_RULE], "UTC", datetime(2026, 11, 30), datetime(2026, 12, 1), [], now=NOW)


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        ("tentative", "confirmed", True),
        ("tentative", "canceled", True),
        ("confirmed", "canceled", True),
        ("confirmed", "completed", True),
        ("confirmed", "no_show", True),
        ("tentative", "completed", False),
        ("canceled", "confirmed", False),
        ("completed", "canceled", False),
        ("no_show", "confirmed", False),
    ],
)
def test_state_machine(current, target, ok):
    assert can_transition(current, target) is ok
    if ok:
        require_transition(current, target)
    else:
        with pytest.raises(Conflict):
            require_transition(current, target)


def test_terminal_states_have_no_exits_and_same_state_is_a_no_op():
    assert {s for s, nxt in TRANSITIONS.items() if not nxt} == {"canceled", "completed", "no_show"}
    require_transition("canceled", "canceled")


def test_event_ids_are_deterministic_and_google_compatible():
    a = event_id_for("t", "a", 1)
    assert a == event_id_for("t", "a", 1) and a != event_id_for("t", "a", 2)
    assert set(a) <= set("abcdefghijklmnopqrstuv0123456789") and 5 <= len(a) <= 1024
    assert event_id_from_key(a) == a
    other = event_id_from_key("Not/Valid-Key!")
    assert set(other) <= set("abcdefghijklmnopqrstuv0123456789")
