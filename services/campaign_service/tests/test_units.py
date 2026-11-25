"""State machine, pacing math, calling window and call-outcome mapping (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from campaign_service.consumers import outcome_for
from campaign_service.dispatcher import bucket_capacity, in_window, refill
from campaign_service.state_machine import TERMINAL, TRANSITIONS, InvalidTransition, allowed_actions, next_state
from campaign_service.validation import limit_problems, schedule_problems

STATES = ("draft", "scheduled", "running", "paused", "completed", "canceled", "failed")
ACTIONS = ("schedule", "unschedule", "launch", "pause", "resume", "complete", "cancel", "fail")


def test_happy_path_and_pause_resume_loop():
    state = "draft"
    for action, expected in (
        ("schedule", "scheduled"),
        ("launch", "running"),
        ("pause", "paused"),
        ("resume", "running"),
        ("pause", "paused"),
        ("resume", "running"),
        ("complete", "completed"),
    ):
        state = next_state(state, action)
        assert state == expected


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("action", ACTIONS)
def test_every_illegal_transition_is_rejected(state, action):
    if (state, action) in TRANSITIONS:
        assert next_state(state, action) == TRANSITIONS[(state, action)]
    else:
        with pytest.raises(InvalidTransition):
            next_state(state, action)


def test_terminal_states_have_no_exit_and_launch_needs_schedule():
    for state in TERMINAL:
        assert allowed_actions(state) == []
    with pytest.raises(InvalidTransition):
        next_state("draft", "launch")
    with pytest.raises(InvalidTransition):
        next_state("paused", "complete")
    assert allowed_actions("running") == ["cancel", "complete", "fail", "pause"]


def test_token_bucket_paces_calls_per_minute():
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    assert bucket_capacity(12) == 1 and bucket_capacity(60) == 5 and bucket_capacity(1) == 1
    assert refill(None, None, 60, now) == 5.0  # starts full (a small burst)
    assert refill(0.0, now, 60, now + timedelta(seconds=2)) == pytest.approx(2.0)
    assert refill(0.0, now, 60, now + timedelta(hours=1)) == 5.0  # never above capacity
    assert refill(0.5, now, 12, now - timedelta(seconds=5)) == 0.5  # clock skew never adds tokens
    # Over any minute, at most capacity + calls_per_minute calls can start.
    tokens, t, started = refill(None, None, 30, now), now, 0
    for _ in range(60):
        tokens = refill(tokens, t - timedelta(seconds=1), 30, t) if started else tokens
        take = int(tokens)
        started, tokens = started + take, tokens - take
        t += timedelta(seconds=1)
    assert started <= bucket_capacity(30) + 30


def test_calling_window_uses_campaign_timezone():
    campaign = {"timezone": "America/New_York", "window_start": "09:00", "window_end": "17:00", "window_days": ["mon"]}
    monday_10_ny = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # 10:00 in New York
    assert in_window(campaign, monday_10_ny)
    assert not in_window(campaign, monday_10_ny.replace(hour=13))  # 08:00 local
    assert not in_window(campaign, monday_10_ny.replace(hour=22))  # 17:00 local: end is exclusive
    assert not in_window(campaign, monday_10_ny + timedelta(days=1))  # Tuesday


def test_call_outcomes_map_to_contact_states():
    assert outcome_for("completed", 1, 3) == ("completed", "completed", None)
    assert outcome_for("no_answer", 1, 3) == ("retry_wait", "no_answer", None)
    assert outcome_for("busy", 3, 3) == ("failed", "busy", None)
    assert outcome_for("opted_out", 1, 3) == ("skipped", "opted_out", "opted_out")
    assert outcome_for("canceled", 1, 3)[0] == "skipped"


def test_local_validation():
    draft = {"agent_id": None, "segment_id": None, "window_start": "09:00", "window_end": "17:00"}
    assert schedule_problems(draft) == ["agent_required", "segment_required"]
    campaign = {
        "max_concurrency": 10,
        "calls_per_minute": 5,
        "window_start": "08:00",
        "window_end": "21:00",
        "window_days": ["sat"],
    }
    comm = {
        "effective_calls_enabled": False,
        "max_concurrent_calls": 5,
        "max_daily_calls": 100,
        "calling_window": {"start": "09:00", "end": "20:00", "days": ["mon"]},
    }
    assert limit_problems(campaign, comm) == [
        "calls_disabled_for_tenant",
        "max_concurrency_exceeds_tenant_limit",
        "window_outside_tenant_calling_window",
        "days_outside_tenant_calling_window",
    ]
