"""Event consumers (called directly, with duplicates and forged producers) and the SSE relay endpoint."""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import select

from nexus_core.authz import ResolvedAccess
from nexus_core.context import Actor
from nexus_core.messaging import EventConsumer, PermanentEventError
from nexus_core.realtime import ui_channel
from nexus_core.testing import auth
from nexus_events import ActorRef, make_event
from notification_service.consumers import ALLOWED_PRODUCERS, make_handlers
from notification_service.domain import notify_channel
from notification_service.tables import inbox, notifications

pytestmark = [pytest.mark.integration, pytest.mark.security]
CONSUMER = Actor("service", "notification_service")

EVENTS = {
    "email.reply_draft.generated": (
        "email-inbox-service",
        {"draft_id": str(uuid.uuid4()), "thread_id": str(uuid.uuid4()), "version": 1},
        "emails:drafts:review",
        "draft_ready",
    ),
    "voice.handoff.requested": ("voice-service", {"call_id": str(uuid.uuid4())}, "calls:manage", "handoff_requested"),
    "webhook.delivery.failed": (
        "integration-service",
        {"endpoint_id": str(uuid.uuid4()), "delivery_id": str(uuid.uuid4()), "attempts": 6},
        "integrations:manage",
        "webhook_failed",
    ),
    "ai.budget.exceeded": (
        "ai-gateway",
        {"scope": "tenant", "period": "2026-11", "limit_micros": 50_000_000, "used_micros": 50_100_000},
        "tenant:manage",
        "ai_budget",
    ),
    "import.completed": (
        "crm-service",
        {"import_id": str(uuid.uuid4()), "total": 10, "created": 8, "updated": 1, "failed": 1},
        "leads:create",
        "import_completed",
    ),
}


def _event(event_type, payload, tenant_id, producer):
    return make_event(
        event_type,
        producer=producer,
        actor=ActorRef(type="service", id=producer),
        correlation_id=uuid.uuid4(),
        tenant_id=tenant_id,
        payload=payload,
    )


def _deliver(run_db, *events):
    handlers = make_handlers(lambda: None)

    async def go(db):
        for ev in events:
            async with db.tenant_session(ev.tenant_id, CONSUMER) as s:
                await handlers[ev.event_type](ev, s)

    run_db(go)


def _rows(run_db, tenant_id):
    async def go(db):
        async with db.tenant_session(tenant_id) as s:
            return (await s.execute(select(notifications).order_by(notifications.c.created_at))).mappings().all()

    return run_db(go)


def test_each_event_type_reaches_its_audience_once(run_db):
    tenant = uuid.uuid4()
    events = [_event(t, p, tenant, prod) for t, (prod, p, _, _) in EVENTS.items()]
    _deliver(run_db, *events, *events)  # every event delivered twice
    rows = _rows(run_db, tenant)
    assert len(rows) == len(EVENTS)
    got = {(r["audience_permission"], r["kind"]) for r in rows}
    assert got == {(perm, kind) for _, _, perm, kind in EVENTS.values()}
    assert all(r["user_id"] is None and r["link"].startswith("/") for r in rows)
    budget = next(r for r in rows if r["kind"] == "ai_budget")
    assert "50.10 of 50.00 USD" in budget["body"]
    assert set(ALLOWED_PRODUCERS) == set(make_handlers(lambda: None))


def test_notification_requested_uses_the_payload_audience(run_db):
    tenant, user = uuid.uuid4(), uuid.uuid4()
    to_user = _event(
        "notification.requested",
        {"template": "booking_reminder", "recipient_user_id": str(user), "link": "/bookings/b1"},
        tenant,
        "booking-service",
    )
    to_perm = _event(
        "notification.requested",
        {"template": "ai_approval_requested", "permission": "ai:approvals:decide"},
        tenant,
        "tool-execution",
    )
    support = _event(
        "notification.requested",
        {"template": "support_access_requested", "resource_type": "support_grant", "resource_id": str(uuid.uuid4()),
         "channel": "email"},
        tenant,
        "tenant-service",
    )  # fmt: skip
    nobody = _event("notification.requested", {"template": "booking_reminder"}, tenant, "booking-service")
    _deliver(run_db, to_user, to_perm, support, nobody, support)
    rows = {r["kind"]: r for r in _rows(run_db, tenant)}
    assert set(rows) == {"booking_reminder", "ai_approval_requested", "support_access_requested"}
    assert rows["booking_reminder"]["user_id"] == user and rows["booking_reminder"]["link"] == "/bookings/b1"
    assert rows["ai_approval_requested"]["audience_permission"] == "ai:approvals:decide"
    assert rows["support_access_requested"]["audience_permission"] == "tenant:manage"


def test_invalid_payloads_and_wildcard_audiences_are_permanent_failures(run_db):
    tenant = uuid.uuid4()
    bad = _event("voice.handoff.requested", {"call_id": str(uuid.uuid4())}, tenant, "voice-service").model_copy(
        update={"payload": {"call_id": "not-a-uuid"}}
    )
    with pytest.raises(PermanentEventError):
        _deliver(run_db, bad)
    wildcard = _event(
        "notification.requested", {"template": "generic", "permission": "calls:*"}, tenant, "booking-service"
    )
    with pytest.raises(PermanentEventError):
        _deliver(run_db, wildcard)


class FakeMessage:
    def __init__(self, event):
        self.body = event.model_dump_json().encode()
        self.headers: dict = {}
        self.acked = self.rejected = False

    async def ack(self):
        self.acked = True

    async def reject(self, requeue=False):
        self.rejected = True


def test_forged_producer_is_ignored_and_redelivery_is_deduped_by_the_inbox(run_db):
    tenant = uuid.uuid4()
    prod, payload, _, _ = EVENTS["ai.budget.exceeded"]
    genuine = _event("ai.budget.exceeded", payload, tenant, prod)
    forged = _event("ai.budget.exceeded", payload, tenant, "crm-service")  # crm may not emit AI budget events

    async def go(db):
        consumer = EventConsumer(
            consumer_name="notification_service",
            db=db,
            inbox=inbox,
            amqp_url="amqp://unused",
            handlers=make_handlers(lambda: None),
            allowed_producers=ALLOWED_PRODUCERS,
        )
        msgs = [FakeMessage(forged), FakeMessage(genuine), FakeMessage(genuine)]
        for m in msgs:
            await consumer.handle_message(m)  # type: ignore[arg-type]
        return msgs

    msgs = run_db(go)
    assert all(m.acked and not m.rejected for m in msgs)
    rows = _rows(run_db, tenant)
    assert len(rows) == 1 and rows[0]["source"] == "ai-gateway"


# --------------------------------------------------------------------------- SSE relay
def _lines(body: str) -> list[str]:
    return [line for line in body.split("\n") if line]


def test_stream_relays_only_what_the_viewer_may_see(harness, fake_redis):
    tenant = uuid.uuid4()
    me, tok = harness.member(tenant, {"calls:read", "emails:drafts:review"})
    mine, theirs, by_perm, other_perm = (uuid.uuid4() for _ in range(4))
    fake_redis.backlog = {
        ui_channel(tenant): [
            json.dumps({"type": "call.updated", "permission": "calls:read", "data": {"call_id": "c1"}}),
            json.dumps({"type": "call.recording_ready", "permission": "calls:recordings:read", "data": {"id": "r"}}),
            json.dumps({"type": "call.updated", "data": {"call_id": "c2"}}),  # untagged: dropped
            "garbage",
        ],
        notify_channel(tenant): [
            json.dumps({"id": str(mine), "kind": "generic", "user_id": str(me), "permission": None}),
            json.dumps({"id": str(theirs), "kind": "generic", "user_id": str(uuid.uuid4()), "permission": None}),
            json.dumps(
                {"id": str(by_perm), "kind": "draft_ready", "user_id": None, "permission": "emails:drafts:review"}
            ),
            json.dumps({"id": str(other_perm), "kind": "x", "user_id": None, "permission": "integrations:manage"}),
        ],
        ui_channel(uuid.uuid4()): [json.dumps({"type": "call.updated", "permission": "calls:read", "data": {}})],
    }  # fmt: skip
    started = time.monotonic()
    r = harness.client.get("/api/v1/notifications/stream", headers=auth(tok, tenant))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store"
    data = [json.loads(line[6:]) for line in _lines(r.text) if line.startswith("data: ")]
    assert {"type": "ui", "event": "call.updated", "data": {"call_id": "c1"}} in data
    assert {"type": "notification", "id": str(mine)} in data
    assert {"type": "notification", "id": str(by_perm)} in data
    assert len(data) == 3
    assert ": heartbeat" in _lines(r.text)
    assert time.monotonic() - started < 5  # bounded by sse_max_seconds
    assert all(sub.closed for sub in fake_redis.subscribers) and fake_redis.slots_released == 1


def test_stream_ends_when_membership_is_revoked(harness, fake_redis):
    tenant = uuid.uuid4()
    me, tok = harness.member(tenant, {"calls:read"})
    calls = {"n": 0}
    real = harness.authz.resolve_tenant

    async def revoking(principal, tenant_id):
        calls["n"] += 1
        if calls["n"] > 1:
            return ResolvedAccess(False, frozenset())
        return await real(principal, tenant_id)

    harness.authz.resolve_tenant = revoking  # type: ignore[method-assign]
    harness.app.state.settings.sse_max_seconds = 10.0
    started = time.monotonic()
    r = harness.client.get("/api/v1/notifications/stream", headers=auth(tok, tenant))
    assert r.status_code == 200
    assert time.monotonic() - started < 3  # closed at the first re-check, long before the 10 s limit
    assert calls["n"] == 2


def test_stream_requires_auth_membership_and_redis(harness):
    tenant = uuid.uuid4()
    _, tok = harness.member(tenant, {"calls:read"})
    assert harness.client.get("/api/v1/notifications/stream").status_code == 401
    assert harness.client.get("/api/v1/notifications/stream", headers=auth(tok, uuid.uuid4())).status_code == 403
    assert harness.client.get("/api/v1/notifications/stream", headers=auth(tok, tenant)).status_code == 503
