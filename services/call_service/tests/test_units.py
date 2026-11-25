"""Unit tests: state machine, pre-dial gate logic, TwiML, stream token (no Docker)."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from call_service.gates import calling_window_gate, country_gate, country_of, is_e164, next_window_start, zone
from call_service.state_machine import (
    LIVE,
    TERMINAL,
    TRANSITIONS,
    can_transition,
    check_transition,
    final_state,
    target_for_provider_status,
)
from call_service.stream_token import mint_stream_token
from call_service.twiml import connect_stream, say_and_hangup

ALL = list(TRANSITIONS)


def test_every_state_is_known_and_terminal_states_are_final():
    assert set(ALL) == {
        "queued",
        "scheduled",
        "initiating",
        "ringing",
        "connected",
        "in_progress",
        "completed",
        "failed",
        "canceled",
        "opted_out",
    }
    for state in TERMINAL:
        assert TRANSITIONS[state] == frozenset()
        for target in ALL:
            assert not can_transition(state, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("queued", "initiating"),
        ("queued", "scheduled"),
        ("scheduled", "queued"),
        ("initiating", "ringing"),
        ("initiating", "queued"),  # reconciliation confirmed the provider has no call
        ("ringing", "connected"),
        ("connected", "in_progress"),
        ("in_progress", "completed"),
        ("in_progress", "opted_out"),
        ("ringing", "failed"),
    ],
)
def test_legal_transitions(current, target):
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("completed", "ringing"),  # late, out-of-order callback
        ("in_progress", "ringing"),
        ("connected", "initiating"),
        ("ringing", "queued"),  # a dialed call can never be re-queued (no second live call)
        ("connected", "queued"),
        ("queued", "completed"),
        ("canceled", "queued"),
        ("ringing", "opted_out"),
    ],
)
def test_illegal_transitions(current, target):
    from call_service.state_machine import IllegalTransition

    with pytest.raises(IllegalTransition):
        check_transition(current, target)


def test_live_calls_can_never_go_back_to_a_dialable_state():
    for state in LIVE - {"initiating"}:
        assert not can_transition(state, "queued") and not can_transition(state, "scheduled")


def test_provider_status_mapping_and_final_state_contract():
    assert target_for_provider_status("ringing") == ("ringing", None)
    assert target_for_provider_status("in-progress") == ("connected", None)
    assert target_for_provider_status("busy") == ("failed", "busy")
    assert target_for_provider_status("no-answer") == ("failed", "no_answer")
    assert target_for_provider_status("bogus") is None
    assert final_state("completed", "completed", opted_out=False) == "completed"
    assert final_state("failed", "busy", opted_out=False) == "busy"
    assert final_state("failed", "no_answer", opted_out=False) == "no_answer"
    assert final_state("completed", "answering_machine", opted_out=False) == "no_answer"
    assert final_state("failed", "provider_failed", opted_out=False) == "failed"
    assert final_state("canceled", "kill_switch", opted_out=False) == "canceled"
    assert final_state("completed", None, opted_out=True) == "opted_out"
    allowed = {"completed", "no_answer", "busy", "failed", "opted_out", "canceled"}
    for state in TERMINAL:
        for reason in (None, "busy", "no_answer", "x"):
            assert final_state(state, reason, opted_out=False) in allowed


def test_calling_window_is_evaluated_in_the_contacts_timezone():
    window = {"start": "09:00", "end": "20:00", "days": ["mon", "tue", "wed", "thu", "fri"]}
    now = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # Wednesday: 10:00 New York, 00:00 Tokyo (Thursday)
    assert calling_window_gate(window, now, "America/New_York").verdict == "proceed"
    tokyo = calling_window_gate(window, now, "Asia/Tokyo")
    assert tokyo.verdict == "reschedule" and tokyo.reason == "outside_calling_window"
    assert tokyo.retry_at == datetime(2026, 11, 26, 0, 0, tzinfo=UTC)  # 09:00 Thursday in Tokyo
    # Friday 21:00 in New York -> next window is Monday 09:00 local.
    friday_night = datetime(2026, 11, 28, 2, 0, tzinfo=UTC)
    nxt = next_window_start(window, friday_night, zone("America/New_York"))
    assert nxt.astimezone(zone("America/New_York")).strftime("%a %H:%M") == "Mon 09:00"
    assert calling_window_gate(None, now, "UTC").verdict == "proceed"
    assert zone("Not/AZone").key == "UTC"


def test_country_gate():
    assert country_of("+12025550123") == "US"
    assert country_of("+15550100001") == "US"  # reserved demo range still attributed to its country code
    assert country_of("+442071838750") == "GB"
    assert country_gate("+442071838750", ["US"]).reason == "country_not_allowed"
    assert country_gate("+12025550123", ["US"]).verdict == "proceed"
    assert country_gate("+1", ["US"]).verdict == "fail"
    assert country_gate(None, ["US"]).reason == "no_phone_number"
    assert is_e164("+12025550123") and not is_e164("2025550123") and not is_e164("+1abc")


def test_twiml_escapes_and_puts_token_in_a_parameter_not_the_url():
    resp = connect_stream("wss://voice.test/media/twilio", 'tok"<&>')
    body = resp.body.decode()
    assert '<Stream url="wss://voice.test/media/twilio">' in body
    assert '<Parameter name="token" value="tok&quot;&lt;&amp;&gt;"/>' in body
    assert "<Hangup/>" in say_and_hangup("Bye <now>").body.decode()
    assert "&lt;now&gt;" in say_and_hangup("Bye <now>").body.decode()


def test_stream_token_is_signed_and_carries_ids_only():
    tid, cid = uuid.uuid4(), uuid.uuid4()
    token = mint_stream_token(
        b"k", tenant_id=tid, call_id=cid, call_sid="CA" + "1" * 32, direction="outbound", agent_id=None, contact_id=None
    )
    body, mac = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert claims["tid"] == str(tid) and claims["cid"] == str(cid) and claims["v"] == 1
    assert set(claims) == {"v", "tid", "cid", "sid", "dir", "aid", "ctid", "exp", "jti"}
    assert mac and token != mint_stream_token(
        b"k", tenant_id=tid, call_id=cid, call_sid="CA" + "1" * 32, direction="outbound", agent_id=None, contact_id=None
    )  # unique jti per token
