"""Campaign lifecycle, launch, dispatcher, call.completed consumer, kill switch, isolation (real database)."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from campaign_service.consumers import ALLOWED_PRODUCERS, HANDLERS
from campaign_service.dispatcher import dispatch_tick
from campaign_service.snapshot import snapshot_audience
from campaign_service.tables import inbox
from nexus_core.messaging import EventConsumer
from nexus_events import ActorRef, make_event

pytestmark = [pytest.mark.integration]
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _campaign(h, headers, **overrides):
    body = {
        "name": f"Spring {uuid.uuid4().hex[:6]}",
        "agent_id": str(uuid.uuid4()),
        "segment_id": str(uuid.uuid4()),
        "timezone": "UTC",
        "window_start": "00:00",
        "window_end": "23:59",
        "window_days": ALL_DAYS,
        "max_concurrency": 2,
        "calls_per_minute": 60,
        "max_attempts": 2,
        "retry_delay_seconds": 60,
        **overrides,
    }
    r = h.client.post("/api/v1/campaigns", headers={**headers, **h.ik()}, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _launched(h, headers, audience: int = 0, **overrides):
    """A running campaign whose audience snapshot has completed."""
    h.remote.audience = [uuid.uuid4() for _ in range(audience)]
    c = _campaign(h, headers, **overrides)
    assert h.client.post(f"/api/v1/campaigns/{c['id']}/schedule", headers=headers).status_code == 200
    r = h.client.post(f"/api/v1/campaigns/{c['id']}/launch", headers={**headers, **h.ik()})
    assert r.status_code == 202, r.text
    name, queue, ctx, ids = h.enqueued[-1]
    assert (name, queue, ids) == ("campaigns.snapshot", "calls.schedule", {"campaign_id": c["id"]})
    assert h.job(lambda d: snapshot_audience(d, ctx, uuid.UUID(c["id"]), page_size=2)) == "completed"
    return c, ctx


def _tick(h, tenant_id, now=None):
    """One dispatcher pass, limited to the test's tenant (other tests' campaigns share the database)."""
    return h.job(lambda d: dispatch_tick(d, now=now or datetime.now(UTC), tenant_ids={tenant_id}))


def _states(h, tenant_id, campaign_id):
    async def q(d):
        async with d.db.tenant_session(tenant_id) as s:
            rows = await s.execute(
                text("SELECT contact_id, state, attempts FROM campaign_contacts WHERE campaign_id = :c"),
                {"c": campaign_id},
            )
            return {str(r.contact_id): (r.state, r.attempts) for r in rows}

    return h.job(q)


def _completed(h, tenant_id, campaign_id, call, final_state, *, producer="call-service", times=1):
    event = make_event(
        "call.completed",
        producer=producer,
        actor=ActorRef(type="service", id=producer),
        correlation_id=uuid.uuid4(),
        tenant_id=tenant_id,
        payload={
            "call_id": call["call_id"],
            "direction": "outbound",
            "final_state": final_state,
            "campaign_id": campaign_id,
            "contact_id": call["contact_id"],
        },
    )

    class Msg:
        def __init__(self):
            self.body, self.headers, self.acked = event.model_dump_json().encode(), {}, False

        async def ack(self):
            self.acked = True

        async def reject(self, requeue=False):
            raise AssertionError("rejected")

    async def deliver(d):
        consumer = EventConsumer(
            consumer_name="campaign_service",
            db=d.db,
            inbox=inbox,
            amqp_url="amqp://unused",
            handlers=HANDLERS,
            allowed_producers=ALLOWED_PRODUCERS,
        )
        for _ in range(times):
            await consumer.handle_message(Msg())

    h.job(deliver)


def _call_id_for(remote, contact_id: str) -> dict:
    sent = [c for c in remote.calls if c["contact_id"] == contact_id]
    key = sent[-1]["idempotency_key"]
    return {"contact_id": contact_id, "call_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key))}


# ------------------------------------------------------------------------------------------ CRUD
def test_crud_versioning_and_idempotent_create(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    key = {"Idempotency-Key": "create-1"}
    body = {"name": "Winter", "timezone": "America/New_York"}
    one = harness.client.post("/api/v1/campaigns", headers={**h, **key}, json=body)
    two = harness.client.post("/api/v1/campaigns", headers={**h, **key}, json=body)
    assert one.status_code == two.status_code == 201 and one.json()["id"] == two.json()["id"]
    assert harness.client.post("/api/v1/campaigns", headers={**h, **key}, json={"name": "Other"}).status_code == 422
    cid = one.json()["id"]
    assert one.json()["state"] == "draft" and one.json()["allowed_actions"] == ["cancel", "schedule"]
    r = harness.client.patch(f"/api/v1/campaigns/{cid}", headers=h, json={"max_concurrency": 3, "expected_version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert (
        harness.client.patch(
            f"/api/v1/campaigns/{cid}", headers=h, json={"max_concurrency": 4, "expected_version": 1}
        ).status_code
        == 409
    )
    bad = harness.client.post(
        "/api/v1/campaigns",
        headers={**h, **harness.ik()},
        json={"name": "x", "window_start": "18:00", "window_end": "09:00"},
    )
    assert bad.status_code == 422
    # Scheduling needs an agent and a segment.
    r = harness.client.post(f"/api/v1/campaigns/{cid}/schedule", headers=h)
    assert r.status_code == 422 and r.json()["problems"] == ["agent_required", "segment_required"]
    assert harness.client.delete(f"/api/v1/campaigns/{cid}", headers=h).status_code == 204
    assert harness.client.get(f"/api/v1/campaigns/{cid}", headers=h).status_code == 404


def test_launch_validates_agent_segment_limits_and_retries_key(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c = _campaign(harness, h, max_concurrency=50)
    harness.client.post(f"/api/v1/campaigns/{c['id']}/schedule", headers=h)
    harness.remote.agent = {"name": "Mail bot", "kind": "email", "status": "inactive"}
    harness.remote.segment_missing = True
    key = {"Idempotency-Key": "launch-1"}
    r = harness.client.post(f"/api/v1/campaigns/{c['id']}/launch", headers={**h, **key})
    assert r.status_code == 422
    assert r.json()["problems"] == [
        "agent_not_voice",
        "agent_not_active",
        "segment_not_found",
        "max_concurrency_exceeds_tenant_limit",
    ]
    # Fix the problems; the same key is retryable after a refusal.
    harness.remote.agent = {"name": "Voice", "kind": "voice", "status": "active"}
    harness.remote.segment_missing = False
    harness.client.post(f"/api/v1/campaigns/{c['id']}/unschedule", headers=h)
    harness.client.patch(f"/api/v1/campaigns/{c['id']}", headers=h, json={"max_concurrency": 5, "expected_version": 3})
    harness.client.post(f"/api/v1/campaigns/{c['id']}/schedule", headers=h)
    ok = harness.client.post(f"/api/v1/campaigns/{c['id']}/launch", headers={**h, **key})
    assert ok.status_code == 202 and ok.json()["state"] == "running"
    replay = harness.client.post(f"/api/v1/campaigns/{c['id']}/launch", headers={**h, **key})
    assert replay.status_code == 202 and replay.json() == ok.json()
    harness.remote.comm = {**harness.remote.comm, "effective_calls_enabled": False}
    d = _campaign(harness, h)
    harness.client.post(f"/api/v1/campaigns/{d['id']}/schedule", headers=h)
    r = harness.client.post(f"/api/v1/campaigns/{d['id']}/launch", headers={**h, **harness.ik()})
    assert r.status_code == 422 and "calls_disabled_for_tenant" in r.json()["problems"]


def test_concurrent_duplicate_launches_produce_one_launch(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c = _campaign(harness, h)
    harness.client.post(f"/api/v1/campaigns/{c['id']}/schedule", headers=h)
    same = {"Idempotency-Key": "same-key"}
    headers = [{**h, **same}] * 4 + [{**h, **harness.ik()} for _ in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(
            pool.map(
                lambda hd: harness.client.post(f"/api/v1/campaigns/{c['id']}/launch", headers=hd).status_code, headers
            )
        )
    assert codes.count(202) >= 1 and set(codes) <= {202, 409}

    async def count(d):
        async with d.db.tenant_session(a) as s:
            launched = await s.execute(
                text("SELECT count(*) FROM campaign_events WHERE campaign_id = :c AND kind = 'state.launch'"),
                {"c": c["id"]},
            )
            events = await s.execute(
                text(
                    "SELECT count(*) FROM outbox WHERE event_type = 'campaign.launched' "
                    "AND envelope->'payload'->>'campaign_id' = :c"
                ),
                {"c": c["id"]},
            )
            return launched.scalar_one(), events.scalar_one()

    assert harness.job(count) == (1, 1)
    assert [e[3] for e in harness.enqueued].count({"campaign_id": c["id"]}) == 1  # one snapshot job


# ------------------------------------------------------------------------------------ dispatcher
def test_pacing_and_concurrency_caps_hold(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c, _ = _launched(harness, h, audience=5, calls_per_minute=12, max_concurrency=2)  # bucket capacity 1
    t0 = datetime.now(UTC)
    assert _tick(harness, a, t0) == 1
    assert _tick(harness, a, t0 + timedelta(seconds=1)) == 0  # only 0.2 tokens refilled
    assert _tick(harness, a, t0 + timedelta(seconds=5)) == 1  # 12/min → one token every 5 s
    assert _tick(harness, a, t0 + timedelta(seconds=30)) == 0  # max_concurrency=2 reached despite tokens
    first = harness.remote.calls[0]
    assert first["idempotency_key"] == f"campaign:{c['id']}:contact:{first['contact_id']}:attempt:1"
    assert first["campaign_id"] == c["id"] and first["tenant"] == str(a) and first["campaign_contact_id"]
    _completed(harness, a, c["id"], _call_id_for(harness.remote, first["contact_id"]), "completed")
    assert _tick(harness, a, t0 + timedelta(seconds=31)) == 1  # a slot was freed
    states = _states(harness, a, c["id"])
    assert sorted(s for s, _ in states.values()) == ["completed", "in_flight", "in_flight", "pending", "pending"]
    # Tenant-wide concurrency also caps (another campaign of the same tenant cannot exceed it).
    c2, _ = _launched(harness, h, audience=3, calls_per_minute=60, max_concurrency=5)
    harness.remote.comm = {**harness.remote.comm, "max_concurrent_calls": 2}  # lowered after launch
    assert _tick(harness, a, t0 + timedelta(seconds=60)) == 0


def test_kill_switch_stops_dispatch_immediately(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c, _ = _launched(harness, h, audience=3)
    assert harness.client.post("/api/v1/campaigns/kill-switch", headers=h, json={"engaged": True}).status_code == 401
    mfa = harness.user(a, mfa=True)
    r = harness.client.post("/api/v1/campaigns/kill-switch", headers=mfa, json={"engaged": True, "reason": "complaint"})
    assert r.status_code == 200 and r.json()["engaged"] is True
    assert _tick(harness, a) == 0 and harness.remote.calls == []
    state = harness.client.get(
        f"/internal/v1/campaigns/{c['id']}/dispatch-state", headers=harness.service("svc-call-service", a)
    ).json()
    assert state == {"campaign_id": c["id"], "state": "running", "dispatch_allowed": False}
    d = _campaign(harness, h)
    harness.client.post(f"/api/v1/campaigns/{d['id']}/schedule", headers=h)
    blocked = harness.client.post(f"/api/v1/campaigns/{d['id']}/launch", headers={**h, **harness.ik()})
    assert blocked.status_code == 422 and blocked.json()["problems"] == ["kill_switch_engaged"]
    harness.client.post("/api/v1/campaigns/kill-switch", headers=mfa, json={"engaged": False})
    assert _tick(harness, a) == 2

    async def kill_events(d):
        async with d.db.global_session() as s:
            return (
                await s.execute(
                    text("SELECT count(*) FROM outbox WHERE event_type = 'campaign.kill_switch' AND tenant_id = :t"),
                    {"t": a},
                )
            ).scalar_one()

    assert harness.job(kill_events) == 2


def test_paused_and_canceled_campaigns_never_dispatch(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c, _ = _launched(harness, h, audience=4)
    assert harness.client.post(f"/api/v1/campaigns/{c['id']}/pause", headers=h).status_code == 200
    assert _tick(harness, a) == 0
    svc = harness.service("svc-call-service", a)
    assert (
        harness.client.get(f"/internal/v1/campaigns/{c['id']}/dispatch-state", headers=svc).json()["dispatch_allowed"]
        is False
    )
    assert harness.client.post(f"/api/v1/campaigns/{c['id']}/resume", headers=h).status_code == 200
    assert _tick(harness, a) == 2
    r = harness.client.post(f"/api/v1/campaigns/{c['id']}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["state"] == "canceled"
    assert len(harness.remote.canceled) == 2  # queued calls canceled through call-service
    assert _tick(harness, a) == 0
    assert set(s for s, _ in _states(harness, a, c["id"]).values()) == {"skipped"}
    assert harness.client.post(f"/api/v1/campaigns/{c['id']}/resume", headers=h).status_code == 409


def test_contactability_refusals_and_transient_failures(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c, _ = _launched(harness, h, audience=3, max_concurrency=5, calls_per_minute=600)
    blocked, refused, flaky = (str(i) for i in harness.remote.audience)
    harness.remote.blocked = {blocked: ["suppressed"]}
    harness.remote.call_status = {refused: (422, {"code": "not_contactable"}), flaky: (503, {"code": "unavailable"})}
    assert _tick(harness, a) == 0
    states = _states(harness, a, c["id"])
    assert states[blocked][0] == "skipped" and states[refused][0] == "skipped"
    assert states[flaky] == ("retry_wait", 0)  # attempt number handed back → same idempotency key next time
    harness.remote.call_status = {}
    assert _tick(harness, a, datetime.now(UTC) + timedelta(seconds=31)) == 1
    keys = [x["idempotency_key"] for x in harness.remote.calls if x["contact_id"] == flaky]
    assert keys == [keys[0], keys[0]] and keys[0].endswith(":attempt:1")


def test_call_completed_retries_dedupes_and_completes_campaign(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c, _ = _launched(harness, h, audience=2, max_attempts=2, retry_delay_seconds=60, max_concurrency=5)
    x, y = (str(i) for i in harness.remote.audience)
    assert _tick(harness, a) == 2
    call_x = _call_id_for(harness.remote, x)
    _completed(harness, a, c["id"], call_x, "no_answer", times=2)  # duplicate delivery: applied once
    _completed(harness, a, c["id"], call_x, "completed")  # a different event for the same finished attempt
    _completed(harness, a, c["id"], _call_id_for(harness.remote, y), "busy", producer="voice-service")  # forged
    states = _states(harness, a, c["id"])
    assert states[x] == ("retry_wait", 1) and states[y] == ("in_flight", 1)
    assert _tick(harness, a) == 0  # retry not due yet
    assert _tick(harness, a, datetime.now(UTC) + timedelta(seconds=61)) == 1
    retry = _call_id_for(harness.remote, x)
    assert harness.remote.calls[-1]["idempotency_key"].endswith(":attempt:2")
    _completed(harness, a, c["id"], retry, "no_answer")  # attempts exhausted → failed
    _completed(harness, a, c["id"], _call_id_for(harness.remote, y), "opted_out")
    states = _states(harness, a, c["id"])
    assert states[x] == ("failed", 2) and states[y] == ("skipped", 1)
    final = harness.client.get(f"/api/v1/campaigns/{c['id']}", headers=h).json()
    assert final["state"] == "completed"
    report = harness.client.get(f"/api/v1/campaigns/{c['id']}/report", headers=h).json()
    assert report["contacts_by_state"] == {"failed": 1, "skipped": 1} and report["calls_dispatched"] == 3
    assert report["outcomes"] == {"no_answer": 1, "opted_out": 1} and report["attempts"] == 3


def test_empty_audience_and_end_time_complete_the_campaign(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c, _ = _launched(harness, h, audience=0)
    _tick(harness, a)
    assert harness.client.get(f"/api/v1/campaigns/{c['id']}", headers=h).json()["state"] == "completed"
    d, _ = _launched(harness, h, audience=2, end_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    _tick(harness, a, datetime.now(UTC) + timedelta(minutes=6))
    assert harness.client.get(f"/api/v1/campaigns/{d['id']}", headers=h).json()["state"] == "completed"
    assert {s for s, _ in _states(harness, a, d["id"]).values()} == {"skipped"}


# ------------------------------------------------------------------------------ permissions/isolation
@pytest.mark.security
@pytest.mark.parametrize(
    ("method", "suffix", "perm"),
    [
        ("get", "", "campaigns:read"),
        ("patch", "", "campaigns:manage"),
        ("delete", "", "campaigns:manage"),
        ("post", "/schedule", "campaigns:manage"),
        ("post", "/pause", "campaigns:manage"),
        ("post", "/cancel", "campaigns:manage"),
        ("post", "/launch", "campaigns:launch"),
        ("get", "/report", "campaigns:read"),
    ],
)
def test_permission_boundaries(harness, tenants, method, suffix, perm):
    a, _ = tenants
    c = _campaign(harness, harness.user(a))
    headers = {
        **harness.user(a, {"campaigns:read", "campaigns:create", "campaigns:manage", "campaigns:launch"} - {perm}),
        **harness.ik(),
    }
    kwargs = {"json": {"name": "x", "expected_version": 1}} if method == "patch" else {}
    r = getattr(harness.client, method)(f"/api/v1/campaigns/{c['id']}{suffix}", headers=headers, **kwargs)
    assert r.status_code == 403 and r.json()["detail"] == f"Missing permission: {perm}"


@pytest.mark.security
def test_create_and_kill_switch_permissions(harness, tenants):
    a, _ = tenants
    reader = harness.user(a, {"campaigns:read"}, mfa=True)
    assert (
        harness.client.post("/api/v1/campaigns", headers={**reader, **harness.ik()}, json={"name": "x"}).status_code
        == 403
    )
    assert (
        harness.client.post("/api/v1/campaigns/kill-switch", headers=reader, json={"engaged": True}).status_code == 403
    )
    assert harness.client.get("/api/v1/campaigns/kill-switch", headers=reader).json()["engaged"] is False


@pytest.mark.security
def test_cross_tenant_isolation(harness, tenants):
    a, b = tenants
    ha, hb = harness.user(a), harness.user(b, mfa=True)
    c = _campaign(harness, ha)
    for method, suffix, kwargs in (
        ("get", "", {}),
        ("patch", "", {"json": {"name": "x", "expected_version": 1}}),
        ("delete", "", {}),
        ("post", "/schedule", {}),
        ("post", "/launch", {"headers_extra": True}),
        ("post", "/cancel", {}),
        ("get", "/report", {}),
        ("get", "/contacts", {}),
    ):
        headers = {**hb, **harness.ik()} if kwargs.pop("headers_extra", False) else hb
        r = getattr(harness.client, method)(f"/api/v1/campaigns/{c['id']}{suffix}", headers=headers, **kwargs)
        assert r.status_code == 404, (method, suffix, r.status_code)
    assert harness.client.get("/api/v1/campaigns", headers=hb).json()["items"] == []
    # Tenant B's kill switch does not affect tenant A; the internal API is tenant-scoped too.
    harness.client.post("/api/v1/campaigns/kill-switch", headers=hb, json={"engaged": True})
    assert harness.client.get("/api/v1/campaigns/kill-switch", headers=ha).json()["engaged"] is False
    svc_b = harness.service("svc-call-service", b)
    assert harness.client.get(f"/internal/v1/campaigns/{c['id']}/dispatch-state", headers=svc_b).status_code == 404
    other = harness.service("svc-email-service", a)
    assert harness.client.get(f"/internal/v1/campaigns/{c['id']}/dispatch-state", headers=other).status_code == 403
    # A forged call.completed for another tenant's campaign changes nothing.
    ca, _ = _launched(harness, ha, audience=1)
    assert _tick(harness, a) == 1
    call = _call_id_for(harness.remote, str(harness.remote.audience[0]))
    _completed(harness, b, ca["id"], call, "completed")
    assert {s for s, _ in _states(harness, a, ca["id"]).values()} == {"in_flight"}
