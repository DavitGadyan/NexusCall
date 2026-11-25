"""call-service behaviour and security boundaries against a real database (restricted role, forced RLS)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from call_service.consumers import on_campaign_stopped, on_kill_switch, on_outcome
from call_service.jobs import dispatch_call, process_provider_event, reconcile_call
from call_service.sweeper import reconcile_due, sweep
from nexus_core.credentials import ResolvedCredentials
from nexus_core.testing import auth
from nexus_events import ActorRef, make_event

pytestmark = [pytest.mark.integration]
SID = "CA" + "f" * 32


def _tenant() -> uuid.UUID:
    return uuid.uuid4()


def _call(h: Any, tenant: uuid.UUID, cid: str) -> dict[str, Any]:
    rows = h.sql("SELECT * FROM calls WHERE id = :i", {"i": cid}, tenant=tenant)
    assert rows, "call not visible in its own tenant"
    return dict(rows[0])


def _queued_call(h: Any, tenant: uuid.UUID, **contact: Any) -> tuple[str, uuid.UUID]:
    h.add_number(tenant, f"+1212{uuid.uuid4().int % 10**7:07d}")
    contact_id = h.remote.add_contact(**contact)
    r = h.create_call(tenant, contact_id)
    assert r.status_code == 201, r.text
    return r.json()["call_id"], contact_id


def _dialed(h: Any, tenant: uuid.UUID, **contact: Any) -> dict[str, Any]:
    cid, _ = _queued_call(h, tenant, **contact)
    assert h.run(dispatch_call, h.ctx(tenant), uuid.UUID(cid)) == "dialing"
    return _call(h, tenant, cid)


# --------------------------------------------------------------------------------------------- internal API
@pytest.mark.security
def test_create_call_idempotency_and_caller_checks(harness: Any):
    h, tenant = harness, _tenant()
    contact = h.remote.add_contact()
    agent = uuid.uuid4()
    first = h.create_call(tenant, contact, key="campaign:x:contact:y:attempt:1", agent_id=agent)
    assert first.status_code == 201 and first.json()["state"] == "queued"
    assert ("calls.dispatch", {"call_id": first.json()["call_id"]}) in h.enqueued
    replay = h.create_call(tenant, contact, key="campaign:x:contact:y:attempt:1", agent_id=agent)
    assert replay.status_code == 201 and replay.json() == first.json()
    mismatch = h.create_call(tenant, contact, key="campaign:x:contact:y:attempt:1", agent_id=uuid.uuid4())
    assert mismatch.status_code == 422
    assert len(h.sql("SELECT id FROM calls", tenant=tenant)) == 1
    missing = h.client.post(
        "/internal/v1/calls",
        headers=auth(h.service(), tenant),
        json={"contact_id": str(contact), "agent_id": str(agent)},
    )
    assert missing.status_code == 422
    body = {"contact_id": str(contact), "agent_id": str(agent)}
    for token in (h.service("svc-crm-service"), h.user(tenant, {"calls:manage"})):
        r = h.client.post("/internal/v1/calls", headers=auth(token, tenant, **{"Idempotency-Key": "k"}), json=body)
        assert r.status_code == 403


def test_create_call_refuses_not_contactable_with_422_code(harness: Any):
    h, tenant = harness, _tenant()
    blocked = h.remote.add_contact(allowed=False, reasons=["suppressed"])
    r = h.create_call(tenant, blocked)
    assert r.status_code == 422 and r.json()["code"] == "not_contactable"
    assert h.sql("SELECT id FROM calls", tenant=tenant) == []
    h.remote.crm_down = True
    assert h.create_call(tenant, h.remote.add_contact()).status_code == 503


def test_scheduled_call_waits_and_cancel(harness: Any):
    h, tenant = harness, _tenant()
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r = h.create_call(tenant, h.remote.add_contact(), scheduled_for=later)
    assert r.json()["state"] == "scheduled"
    cid = r.json()["call_id"]
    assert ("calls.dispatch", {"call_id": cid}) not in h.enqueued
    c = h.client.post(
        f"/internal/v1/calls/{cid}/cancel", headers=auth(h.service(), tenant), json={"reason": "campaign_canceled"}
    )
    assert c.status_code == 200 and c.json()["state"] == "canceled"
    completed = h.events("call.completed", cid)
    assert completed[-1]["payload"]["final_state"] == "canceled"
    # Another tenant cannot see or cancel it.
    other = h.client.post(
        f"/internal/v1/calls/{cid}/cancel", headers=auth(h.service(), _tenant()), json={"reason": "x"}
    )
    assert other.status_code == 404


# --------------------------------------------------------------------------------------------- dispatch gates
def test_dispatch_happy_path_dials_once(harness: Any):
    h, tenant = harness, _tenant()
    call = _dialed(h, tenant)
    assert call["state"] == "initiating" and call["provider_call_id"].startswith("CA")
    assert call["from_e164"] and call["to_e164"] == "+12125550123"
    assert h.telephony.creates[-1]["token"] == f"{call['id']}:1"
    # A duplicate delivery of the dispatch task does nothing.
    assert h.run(dispatch_call, h.ctx(tenant), call["id"]) == "skipped"
    assert h.telephony.live_provider_calls_for(call["id"]) == 1


@pytest.mark.parametrize(
    ("setup", "verdict", "state"),
    [
        (
            lambda h, t: h.remote.communication(str(t)).update(effective_calls_enabled=False),
            "cancel:calls_disabled",
            "canceled",
        ),
        (
            lambda h, t: h.remote.communication(str(t)).update(allowed_call_countries=["GB"]),
            "cancel:country_not_allowed",
            "canceled",
        ),
        (
            lambda h, t: h.remote.communication(str(t)).update(max_daily_calls=0),
            "reschedule:daily_cap_reached",
            "scheduled",
        ),
        (
            lambda h, t: h.remote.communication(str(t)).update(max_concurrent_calls=0),
            "reschedule:concurrency_limit",
            "scheduled",
        ),
        (
            lambda h, t: h.remote.communication(str(t)).update(
                calling_window={"start": "00:00", "end": "00:01", "days": ["mon"]}
            ),
            "reschedule:outside_calling_window",
            "scheduled",
        ),
    ],
    ids=["kill_switch", "country", "daily_cap", "concurrency", "calling_window"],
)
def test_pre_dial_gates_block_and_record_a_reason(harness: Any, setup, verdict, state):
    h, tenant = harness, _tenant()
    cid, _ = _queued_call(h, tenant)
    setup(h, tenant)
    assert h.run(dispatch_call, h.ctx(tenant), uuid.UUID(cid)) == verdict
    row = _call(h, tenant, cid)
    assert row["state"] == state and row["blocked_reason"] == verdict.split(":", 1)[1]
    assert h.telephony.creates == []
    if state == "scheduled":
        assert row["scheduled_for"] > datetime.now(UTC)


def test_gate_campaign_paused_and_contact_no_longer_contactable(harness: Any):
    h, tenant = harness, _tenant()
    h.add_number(tenant, "+12125559001")
    contact = h.remote.add_contact()
    campaign = uuid.uuid4()
    cid = h.create_call(tenant, contact, campaign_id=str(campaign)).json()["call_id"]
    h.remote.campaigns[str(campaign)] = {"state": "paused", "dispatch_allowed": False}
    assert h.run(dispatch_call, h.ctx(tenant), uuid.UUID(cid)) == "cancel:campaign_paused"
    cid2 = h.create_call(tenant, contact).json()["call_id"]
    h.remote.contacts[str(contact)].update(allowed=False, reasons=["suppressed"], phone_e164=None)
    assert h.run(dispatch_call, h.ctx(tenant), uuid.UUID(cid2)) == "cancel:not_contactable:suppressed"
    assert h.telephony.creates == []


def test_calling_window_uses_contact_timezone(harness: Any):
    h, tenant = harness, _tenant()
    now = datetime.now(UTC)
    # A one-hour window around "now" in UTC: open for a UTC contact, closed for a contact 12 hours away.
    start = now.strftime("%H:00")
    end = (now + timedelta(hours=1)).strftime("%H:00") if now.hour < 23 else "23:59"
    h.remote.communication(str(tenant))["calling_window"] = {
        "start": start,
        "end": end,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    }
    utc_call, _ = _queued_call(h, tenant, tz="UTC")
    far_call, _ = _queued_call(h, tenant, tz="Pacific/Kiritimati")  # UTC+14
    assert h.run(dispatch_call, h.ctx(tenant), uuid.UUID(utc_call)) == "dialing"
    assert h.run(dispatch_call, h.ctx(tenant), uuid.UUID(far_call)) == "reschedule:outside_calling_window"


def test_provider_rejection_fails_without_retry(harness: Any):
    h, tenant = harness, _tenant()
    h.telephony.modes["+12125550111"] = "rejected"
    cid, _ = _queued_call(h, tenant, phone="+12125550111")
    assert h.run(dispatch_call, h.ctx(tenant), uuid.UUID(cid)) == "fail:provider_rejected"
    row = _call(h, tenant, cid)
    assert row["state"] == "failed" and row["end_reason"] == "provider_rejected"
    assert h.events("call.completed", cid)[-1]["payload"]["final_state"] == "failed"


# --------------------------------------------------------------------------------------------- ambiguous results
def test_ambiguous_timeout_is_reconciled_and_never_duplicated(harness: Any):
    h, tenant = harness, _tenant()
    h.telephony.modes["+12125550197"] = "ambiguous_created"
    cid, _ = _queued_call(h, tenant, phone="+12125550197")
    call_id = uuid.UUID(cid)
    assert h.run(dispatch_call, h.ctx(tenant), call_id) == "ambiguous"
    row = _call(h, tenant, cid)
    assert row["state"] == "initiating" and row["provider_call_id"] is None
    attempt = h.sql("SELECT status FROM call_attempts WHERE call_id = :c", {"c": cid}, tenant=tenant)
    assert attempt[0]["status"] == "ambiguous"
    later = datetime.now(UTC) + timedelta(minutes=2)
    assert h.run(reconcile_call, h.ctx(tenant), call_id, now=later) == "found"
    row = _call(h, tenant, cid)
    assert row["provider_call_id"] is not None and row["state"] in ("initiating", "ringing")
    assert len(h.telephony.creates) == 1 and h.telephony.live_provider_calls_for(call_id) == 1
    # Nothing re-dials it: dispatch does not claim a non-queued call and reconciliation is a no-op now.
    assert h.run(dispatch_call, h.ctx(tenant), call_id) == "skipped"
    assert h.run(reconcile_call, h.ctx(tenant), call_id, now=later) in ("created", "nothing_to_do", "still_live")
    assert len(h.telephony.creates) == 1


def test_ambiguous_without_provider_call_is_retried_once_confirmed_absent(harness: Any):
    h, tenant = harness, _tenant()
    h.telephony.modes["+12125550194"] = "ambiguous_absent"
    cid, _ = _queued_call(h, tenant, phone="+12125550194")
    call_id = uuid.UUID(cid)
    assert h.run(dispatch_call, h.ctx(tenant), call_id) == "ambiguous"
    # While the outcome is unknown, reconciliation that cannot reach the provider must NOT re-dial.
    h.telephony.lookup_fails = True
    assert h.run(reconcile_call, h.ctx(tenant), call_id, now=datetime.now(UTC) + timedelta(minutes=2)) == (
        "still_ambiguous"
    )
    assert _call(h, tenant, cid)["state"] == "initiating"
    h.telephony.lookup_fails = False
    result = h.run(reconcile_call, h.ctx(tenant), call_id, now=datetime.now(UTC) + timedelta(minutes=2))
    assert result == "requeued_after_reconcile"
    assert _call(h, tenant, cid)["state"] == "queued"
    assert ("calls.dispatch", {"call_id": cid}) in h.enqueued
    assert h.run(dispatch_call, h.ctx(tenant), call_id) == "dialing"
    tokens = [c["token"] for c in h.telephony.creates]
    assert tokens == [f"{cid}:1", f"{cid}:2"]
    assert h.telephony.live_provider_calls_for(call_id) == 1


def test_reconciliation_job_finds_stuck_calls_across_tenants(harness: Any):
    h, tenant = harness, _tenant()
    h.telephony.modes["+12125550197"] = "ambiguous_created"
    cid, _ = _queued_call(h, tenant, phone="+12125550197")
    h.run(dispatch_call, h.ctx(tenant), uuid.UUID(cid))
    h.enqueued.clear()

    async def job(deps):
        return await reconcile_due(deps, now=datetime.now(UTC) + timedelta(minutes=5))

    assert h.run(job) >= 1
    assert ("calls.reconcile", {"call_id": cid}) in h.enqueued


def test_sweeper_requeues_due_scheduled_calls(harness: Any):
    h, tenant = harness, _tenant()
    soon = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    cid = h.create_call(tenant, h.remote.add_contact(), scheduled_for=soon).json()["call_id"]
    h.enqueued.clear()

    async def job(deps):
        return await sweep(deps, now=datetime.now(UTC) + timedelta(minutes=1))

    assert h.run(job) >= 1
    assert _call(h, tenant, cid)["state"] == "queued"
    assert ("calls.dispatch", {"call_id": cid}) in h.enqueued


# --------------------------------------------------------------------------------------------- webhooks
def _status(h: Any, call: dict[str, Any], status: str, seq: int, **extra: str) -> Any:
    params = {"CallSid": call["provider_call_id"], "CallStatus": status, "SequenceNumber": str(seq), **extra}
    return h.post_webhook(f"/webhooks/twilio/status?c={call['id']}", params)


def _process_all(h: Any, tenant: uuid.UUID) -> list[str]:
    results = []
    for name, ids in list(h.enqueued):
        if name == "calls.provider_callback":
            results.append(h.run(process_provider_event, h.ctx(tenant), uuid.UUID(ids["event_id"])))
    h.enqueued.clear()
    return results


@pytest.mark.security
def test_status_webhook_signature_is_required(harness: Any):
    h, tenant = harness, _tenant()
    call = _dialed(h, tenant)
    params = {"CallSid": call["provider_call_id"], "CallStatus": "ringing", "SequenceNumber": "0"}
    path = f"/webhooks/twilio/status?c={call['id']}"
    assert h.post_webhook(path, params, secret="wrong-secret").status_code == 403
    assert h.post_webhook(path, params, signature="").status_code == 403
    # Signature computed for a different URL (e.g. another call id) does not verify here.
    other = h.sign(f"http://call.test/webhooks/twilio/status?c={uuid.uuid4()}", params)
    assert h.client.post(path, data=params, headers=other).status_code == 403
    # Tampered payload after signing.
    signed = h.sign(f"http://call.test{path}", params)
    assert h.client.post(path, data={**params, "CallStatus": "completed"}, headers=signed).status_code == 403
    assert h.post_webhook(f"/webhooks/twilio/status?c={uuid.uuid4()}", params).status_code == 404
    assert h.sql("SELECT id FROM provider_events WHERE call_id = :c", {"c": str(call["id"])}, tenant=tenant) == []


@pytest.mark.security
def test_duplicate_and_out_of_order_callbacks_are_safe(harness: Any):
    h, tenant = harness, _tenant()
    call = _dialed(h, tenant)
    h.enqueued.clear()
    assert _status(h, call, "ringing", 1).status_code == 204
    assert _status(h, call, "ringing", 1).status_code == 204  # duplicate delivery
    events = h.sql("SELECT id FROM provider_events WHERE call_id = :c", {"c": str(call["id"])}, tenant=tenant)
    assert len(events) == 1
    assert _process_all(h, tenant) == ["applied"]
    assert _status(h, call, "completed", 3, CallDuration="42", Price="-0.0130").status_code == 204
    assert _status(h, call, "in-progress", 2).status_code == 204  # arrives late
    results = _process_all(h, tenant)
    assert results[0] == "applied" and results[1].startswith("ignored")
    row = _call(h, tenant, str(call["id"]))
    assert row["state"] == "completed" and row["duration_seconds"] == 42 and row["cost_micros"] == 13000
    completed = h.events("call.completed", call["id"])
    assert len(completed) == 1 and completed[0]["payload"]["final_state"] == "completed"
    # Re-running the same event is a no-op.
    any_event = h.sql("SELECT id FROM provider_events WHERE call_id = :c", {"c": str(call["id"])}, tenant=tenant)
    assert h.run(process_provider_event, h.ctx(tenant), any_event[0]["id"]) == "skipped"


def test_busy_and_no_answer_map_to_campaign_final_states(harness: Any):
    h, tenant = harness, _tenant()
    for status, final in (("busy", "busy"), ("no-answer", "no_answer")):
        call = _dialed(h, tenant)
        h.enqueued.clear()
        _status(h, call, status, 1)
        _process_all(h, tenant)
        assert h.events("call.completed", call["id"])[0]["payload"]["final_state"] == final


def test_answer_webhook_connects_stream_with_token(harness: Any):
    h, tenant = harness, _tenant()
    call = _dialed(h, tenant)
    params = {"CallSid": call["provider_call_id"], "CallStatus": "in-progress"}
    r = h.post_webhook(f"/webhooks/twilio/answer?c={call['id']}", params)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert '<Stream url="wss://voice.test/media/twilio">' in r.text and 'name="token"' in r.text
    assert "?" not in r.text.split('url="')[1].split('"')[0]  # never a token in the URL
    assert _call(h, tenant, str(call["id"]))["state"] == "connected"
    assert h.post_webhook(f"/webhooks/twilio/answer?c={call['id']}", params, secret="x").status_code == 403
    machine = _dialed(h, tenant)
    r = h.post_webhook(
        f"/webhooks/twilio/answer?c={machine['id']}",
        {"CallSid": machine["provider_call_id"], "AnsweredBy": "machine_start"},
    )
    assert "<Hangup/>" in r.text


# --------------------------------------------------------------------------------------------- inbound
def _inbound(h: Any, to: str, *, sid: str | None = None, secret: str | None = None, caller: str = "+12125550177"):
    params = {"CallSid": sid or ("CA" + uuid.uuid4().hex), "To": to, "From": caller, "Direction": "inbound"}
    kwargs = {"secret": secret} if secret else {}
    return h.post_webhook("/webhooks/twilio/voice", params, **kwargs), params["CallSid"]


def test_inbound_call_is_routed_by_the_dialed_number(harness: Any):
    h, tenant = harness, _tenant()
    agent = uuid.uuid4()
    number = h.add_number(tenant, "+13125550142", agent_id=agent)
    contact = uuid.uuid4()
    h.remote.lookup["+12125550177"] = str(contact)
    r, sid = _inbound(h, "+13125550142")
    assert r.status_code == 200 and "<Connect><Stream" in r.text
    rows = h.sql("SELECT * FROM calls WHERE provider_call_id = :s", {"s": sid}, tenant=tenant)
    assert len(rows) == 1
    call = rows[0]
    assert call["direction"] == "inbound" and call["state"] == "connected"
    assert (
        call["agent_id"] == agent and call["contact_id"] == contact and str(call["inbound_number_id"]) == number["id"]
    )
    again, _ = _inbound(h, "+13125550142", sid=sid)  # Twilio retry: same call, no duplicate
    assert "<Connect><Stream" in again.text
    assert len(h.sql("SELECT id FROM calls WHERE provider_call_id = :s", {"s": sid}, tenant=tenant)) == 1


def test_inbound_unknown_or_disabled_is_rejected_politely(harness: Any):
    h, tenant = harness, _tenant()
    r, _ = _inbound(h, "+13125559999")
    assert r.status_code == 200 and "<Say>" in r.text and "<Hangup/>" in r.text and "<Stream" not in r.text
    h.add_number(tenant, "+13125550143")
    h.remote.communication(str(tenant))["effective_calls_enabled"] = False
    r, sid = _inbound(h, "+13125550143")
    assert "<Hangup/>" in r.text and "<Stream" not in r.text
    assert h.sql("SELECT id FROM calls WHERE provider_call_id = :s", {"s": sid}, tenant=tenant) == []


@pytest.mark.security
def test_inbound_tenant_cannot_be_spoofed(harness: Any):
    """Tenant B's number with a signature made with tenant A's token (or with payload hints) is refused."""
    h = harness
    tenant_a, tenant_b = _tenant(), _tenant()
    h.add_number(tenant_b, "+13125550144")

    class PerTenant:
        async def resolve(self, tenant_id, provider, **_):
            return ResolvedCredentials(
                provider,
                "conn",
                {"account_sid": SecretStr("AC" + "a" * 32), "auth_token": SecretStr(f"tok-{tenant_id}")},
                {},
                "tenant",
            )

    h.app.state.credentials = PerTenant()
    params = {
        "CallSid": "CA" + uuid.uuid4().hex,
        "To": "+13125550144",
        "From": "+12125550177",
        "AccountSid": "AC" + "a" * 32,
        "TenantId": str(tenant_a),
    }
    r = h.post_webhook("/webhooks/twilio/voice", params, secret=f"tok-{tenant_a}")
    assert r.status_code == 403
    assert h.sql("SELECT id FROM calls", tenant=tenant_a) == [] and h.sql("SELECT id FROM calls", tenant=tenant_b) == []
    ok = h.post_webhook("/webhooks/twilio/voice", params, secret=f"tok-{tenant_b}")
    assert ok.status_code == 200 and "<Stream" in ok.text
    assert (
        len(h.sql("SELECT id FROM calls", tenant=tenant_b)) == 1
        and h.sql("SELECT id FROM calls", tenant=tenant_a) == []
    )


# --------------------------------------------------------------------------------------------- voice hooks
def test_opt_out_then_completion_ends_as_opted_out(harness: Any):
    h, tenant = harness, _tenant()
    call = _dialed(h, tenant)
    voice = h.service("svc-voice-service")
    assert (
        h.client.post(f"/internal/v1/calls/{call['id']}/media-started", headers=auth(voice, tenant)).json()["state"]
        == "in_progress"
    )
    r = h.client.post(f"/internal/v1/calls/{call['id']}/opt-out", headers=auth(voice, tenant))
    assert r.status_code == 200 and r.json()["opted_out"] is True
    h.client.post(f"/internal/v1/calls/{call['id']}/opt-out", headers=auth(voice, tenant))  # idempotent
    assert len(h.events("call.opted_out", call["id"])) == 1
    assert (
        h.client.post(f"/internal/v1/calls/{call['id']}/opt-out", headers=auth(h.service(), tenant)).status_code == 403
    )
    h.enqueued.clear()
    _status(h, call, "completed", 5)
    _process_all(h, tenant)
    row = _call(h, tenant, str(call["id"]))
    assert row["state"] == "opted_out" and row["opted_out"] is True
    assert h.events("call.completed", call["id"])[0]["payload"]["final_state"] == "opted_out"


def test_transfer_to_human(harness: Any):
    h, tenant = harness, _tenant()
    h.add_number(tenant, "+12125558001", handoff_e164="+12125558002")
    contact = h.remote.add_contact()
    cid = h.create_call(tenant, contact).json()["call_id"]
    h.run(dispatch_call, h.ctx(tenant), uuid.UUID(cid))
    call = _call(h, tenant, cid)
    tool = h.service("svc-tool-execution")
    headers = auth(tool, tenant, **{"Idempotency-Key": f"t-{cid}"})
    not_live = h.client.post(f"/internal/v1/calls/{cid}/transfer", headers=headers, json={"reason": "caller_request"})
    assert not_live.status_code == 200 and not_live.json()["status"] == "unavailable"
    h.post_webhook(f"/webhooks/twilio/answer?c={cid}", {"CallSid": call["provider_call_id"]})
    headers = auth(tool, tenant, **{"Idempotency-Key": f"t2-{cid}"})
    r = h.client.post(f"/internal/v1/calls/{cid}/transfer", headers=headers, json={"reason": "caller_request"})
    assert r.json() == {"status": "transferring"}
    assert h.telephony.transfers == [(call["provider_call_id"], f"handoff-{cid}")]
    assert h.telephony.dials[0]["to"] == "+12125558002"
    replay = h.client.post(f"/internal/v1/calls/{cid}/transfer", headers=headers, json={"reason": "caller_request"})
    assert replay.json() == {"status": "transferring"} and len(h.telephony.transfers) == 1
    assert (
        h.client.post(
            f"/internal/v1/calls/{cid}/transfer",
            headers=auth(h.service(), tenant, **{"Idempotency-Key": "z"}),
            json={"reason": "x"},
        ).status_code
        == 403
    )
    info = h.client.get(f"/internal/v1/calls/{cid}", headers=auth(h.service("svc-voice-service"), tenant)).json()
    assert set(info) == {
        "call_id",
        "state",
        "direction",
        "contact_id",
        "agent_id",
        "campaign_id",
        "started_at",
        "ended_at",
    }


# --------------------------------------------------------------------------------------------- public API
@pytest.mark.security
def test_public_call_api_permissions_and_isolation(harness: Any):
    h = harness
    tenant_a, tenant_b = _tenant(), _tenant()
    call = _dialed(h, tenant_a)
    analyst = h.user(tenant_a, {"calls:read"})
    manager = h.user(tenant_a, {"calls:read", "calls:manage"})
    outsider = h.user(tenant_b, {"calls:read", "calls:manage"})
    nobody = h.user(tenant_a, {"leads:read"})
    page = h.client.get("/api/v1/calls", headers=auth(analyst, tenant_a)).json()
    assert [i["id"] for i in page["items"]] == [str(call["id"])]
    item = page["items"][0]
    assert "to_e164" not in item and item["to_masked"].endswith("23") and "*" in item["to_masked"]
    assert h.client.get(f"/api/v1/calls/{call['id']}", headers=auth(analyst, tenant_a)).status_code == 200
    assert h.client.get("/api/v1/calls/live", headers=auth(analyst, tenant_a)).json()["items"][0]["id"] == str(
        call["id"]
    )
    assert h.client.get("/api/v1/calls", headers=auth(nobody, tenant_a)).status_code == 403
    # Cross-tenant: tenant B sees nothing and gets 404 for A's call, even with calls:manage.
    assert h.client.get("/api/v1/calls", headers=auth(outsider, tenant_b)).json()["items"] == []
    assert h.client.get(f"/api/v1/calls/{call['id']}", headers=auth(outsider, tenant_b)).status_code == 404
    assert h.client.post(f"/api/v1/calls/{call['id']}/hangup", headers=auth(outsider, tenant_b)).status_code == 404
    assert h.client.get(f"/api/v1/calls/{call['id']}", headers=auth(outsider, tenant_a)).status_code == 403
    # Hangup needs calls:manage and is audited.
    assert h.client.post(f"/api/v1/calls/{call['id']}/hangup", headers=auth(analyst, tenant_a)).status_code == 403
    r = h.client.post(f"/api/v1/calls/{call['id']}/hangup", headers=auth(manager, tenant_a))
    assert r.status_code == 200 and r.json()["hangup"] == "requested"
    assert ("calls.hangup", {"call_id": str(call["id"])}) in h.enqueued
    audits = h.sql("SELECT envelope FROM outbox WHERE event_type = 'audit.event'")
    assert any(a["envelope"]["payload"]["action"] == "call.hangup" for a in audits)
    # RLS: without tenant context no call rows are visible at all.
    assert h.sql("SELECT id FROM calls") == []


def test_list_pagination_and_filters(harness: Any):
    h, tenant = harness, _tenant()
    for _ in range(3):
        h.create_call(tenant, h.remote.add_contact())
    reader = h.user(tenant, {"calls:read"})
    first = h.client.get("/api/v1/calls?limit=2", headers=auth(reader, tenant)).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = h.client.get(f"/api/v1/calls?limit=2&cursor={first['next_cursor']}", headers=auth(reader, tenant)).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert h.client.get("/api/v1/calls?state=completed", headers=auth(reader, tenant)).json()["items"] == []
    assert h.client.get("/api/v1/calls?limit=500", headers=auth(reader, tenant)).status_code == 422


def test_test_call_requires_manage_and_idempotency(harness: Any):
    h, tenant = harness, _tenant()
    contact = h.remote.add_contact()
    manager = h.user(tenant, {"calls:manage"})
    body = {"contact_id": str(contact), "agent_id": str(uuid.uuid4())}
    assert h.client.post("/api/v1/calls/test", headers=auth(manager, tenant), json=body).status_code == 422
    hdr = auth(manager, tenant, **{"Idempotency-Key": "test-1"})
    r = h.client.post("/api/v1/calls/test", headers=hdr, json=body)
    assert r.status_code == 201
    assert h.client.post("/api/v1/calls/test", headers=hdr, json=body).json() == r.json()
    assert _call(h, tenant, r.json()["call_id"])["is_test"] is True
    reader = h.user(tenant, {"calls:read"})
    assert (
        h.client.post(
            "/api/v1/calls/test", headers=auth(reader, tenant, **{"Idempotency-Key": "t2"}), json=body
        ).status_code
        == 403
    )


@pytest.mark.security
def test_inbound_number_crud_permissions_and_isolation(harness: Any):
    h = harness
    tenant_a, tenant_b = _tenant(), _tenant()
    number = h.add_number(tenant_a, "+13125550155")
    admin_b = h.user(tenant_b, {"calls:manage"})
    reader_a = h.user(tenant_a, {"calls:read"})
    admin_a = h.user(tenant_a, {"calls:manage"})
    taken = h.client.post(
        "/api/v1/inbound-numbers",
        headers=auth(admin_b, tenant_b),
        json={"e164": "+13125550155", "agent_id": str(uuid.uuid4())},
    )
    assert taken.status_code == 409
    assert h.client.get("/api/v1/inbound-numbers", headers=auth(reader_a, tenant_a)).status_code == 403
    assert h.client.get("/api/v1/inbound-numbers", headers=auth(admin_b, tenant_b)).json()["items"] == []
    for method in ("get", "delete"):
        assert (
            getattr(h.client, method)(
                f"/api/v1/inbound-numbers/{number['id']}", headers=auth(admin_b, tenant_b)
            ).status_code
            == 404
        )
    patch = {"label": "Sales", "expected_version": number["version"]}
    assert (
        h.client.patch(
            f"/api/v1/inbound-numbers/{number['id']}", headers=auth(admin_b, tenant_b), json=patch
        ).status_code
        == 404
    )
    ok = h.client.patch(f"/api/v1/inbound-numbers/{number['id']}", headers=auth(admin_a, tenant_a), json=patch)
    assert ok.status_code == 200 and ok.json()["version"] == number["version"] + 1
    stale = h.client.patch(f"/api/v1/inbound-numbers/{number['id']}", headers=auth(admin_a, tenant_a), json=patch)
    assert stale.status_code == 409
    bad = h.client.post(
        "/api/v1/inbound-numbers",
        headers=auth(admin_a, tenant_a),
        json={"e164": "12345", "agent_id": str(uuid.uuid4())},
    )
    assert bad.status_code == 422
    disabled = h.client.patch(
        f"/api/v1/inbound-numbers/{number['id']}",
        headers=auth(admin_a, tenant_a),
        json={"status": "disabled", "expected_version": number["version"] + 1},
    )
    assert disabled.status_code == 200
    assert h.sql("SELECT status FROM number_directory WHERE e164 = '+13125550155'")[0]["status"] == "disabled"
    assert (
        h.client.delete(f"/api/v1/inbound-numbers/{number['id']}", headers=auth(admin_a, tenant_a)).status_code == 204
    )
    assert h.sql("SELECT e164 FROM number_directory WHERE e164 = '+13125550155'") == []
    audits = [
        a["envelope"]["payload"]["action"]
        for a in h.sql("SELECT envelope FROM outbox WHERE event_type = 'audit.event'")
    ]
    assert {"inbound_number.created", "inbound_number.updated", "inbound_number.deleted"} <= set(audits)


# --------------------------------------------------------------------------------------------- consumers
def _event(event_type: str, tenant: uuid.UUID, payload: dict[str, Any], producer: str = "campaign-service"):
    return make_event(
        event_type,
        producer=producer,
        actor=ActorRef(type="service", id=producer),
        correlation_id=uuid.uuid4(),
        tenant_id=tenant,
        payload=payload,
    )


def test_campaign_events_cancel_queued_calls_and_duplicates_are_noops(harness: Any):
    h, tenant = harness, _tenant()
    campaign = uuid.uuid4()
    mine = [
        h.create_call(tenant, h.remote.add_contact(), campaign_id=str(campaign)).json()["call_id"] for _ in range(2)
    ]
    other = h.create_call(tenant, h.remote.add_contact()).json()["call_id"]
    event = _event("campaign.canceled", tenant, {"campaign_id": str(campaign), "channel": "call"})

    async def handle(deps):
        async with deps.db.tenant_session(tenant) as s:
            await on_campaign_stopped(event, s)
        async with deps.db.tenant_session(tenant) as s:
            await on_campaign_stopped(event, s)  # duplicate delivery

    h.run(handle)
    for cid in mine:
        assert _call(h, tenant, cid)["state"] == "canceled"
        assert len(h.events("call.completed", cid)) == 1
    assert _call(h, tenant, other)["state"] == "queued"
    kill = _event("campaign.kill_switch", tenant, {"scope": "tenant", "channel": "call", "engaged": True})

    async def handle_kill(deps):
        async with deps.db.tenant_session(tenant) as s:
            await on_kill_switch(kill, s)

    h.run(handle_kill)
    assert _call(h, tenant, other)["state"] == "canceled" and _call(h, tenant, other)["end_reason"] == "kill_switch"


def test_outcome_consumer_records_and_ignores_unknown_calls(harness: Any):
    h, tenant = harness, _tenant()
    call = _dialed(h, tenant)
    good = _event(
        "call.outcome.extracted",
        tenant,
        {"call_id": str(call["id"]), "outcome": "booked", "qualified": True},
        "ai-agent-service",
    )
    unknown = _event(
        "call.outcome.extracted", tenant, {"call_id": str(uuid.uuid4()), "outcome": "x"}, "ai-agent-service"
    )

    async def handle(deps):
        for ev in (good, good, unknown):
            async with deps.db.tenant_session(tenant) as s:
                await on_outcome(ev, s)

    h.run(handle)
    rows = h.sql("SELECT outcome, qualified FROM call_outcomes", tenant=tenant)
    assert [dict(r) for r in rows] == [{"outcome": "booked", "qualified": True}]
    reader = h.user(tenant, {"calls:read"})
    assert h.client.get(f"/api/v1/calls/{call['id']}", headers=auth(reader, tenant)).json()["outcome"] == "booked"
