"""CSV import pipeline, event consumers (duplicate and forged deliveries), sweeper and demo seed."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from crm_service.consumers import ALLOWED_PRODUCERS, HANDLERS
from crm_service.jobs import process_import, sweep_stuck_jobs
from crm_service.normalize import value_hash
from crm_service.tables import inbox
from nexus_core.messaging import EventConsumer
from nexus_events import ActorRef, make_event

pytestmark = [pytest.mark.integration]

CSV = (
    "email,phone,first_name,company,custom.plan,country\n"
    "new1@example.test,212 555 0171,Nia,Acme,pro,US\n"
    "NEW1@example.test,,Nia Updated,,,\n"
    ",+1 212 555 0172,Existing By Phone,,,\n"
    "not-an-email,,Bad,,,\n"
    ",12,Bad Phone,,,\n"
    ",,No Address,,,\n"
    "plan@example.test,,Bad Plan,,gold,\n"
)


def _upload(h, headers, data: bytes, options: dict | None = None, name: str = "leads.csv"):
    return h.client.post(
        "/api/v1/imports",
        headers={**headers, **h.ik()},
        files={"file": (name, data, "text/csv")},
        data={"options": json.dumps(options or {})},
    )


async def _scalar(h, sql: str, tenant_id):
    db = h.database()
    try:
        async with db.tenant_session(tenant_id) as s:
            return (await s.execute(text(sql))).scalar_one()
    finally:
        await db.dispose()


def test_csv_import_validates_dedupes_captures_consent_and_reports(harness, tenants):
    a, _ = tenants
    user = harness.user(a)
    harness.client.post(
        "/api/v1/custom-fields",
        headers=user,
        json={"key": "plan", "label": "Plan", "type": "select", "options": ["pro"]},
    )
    existing = harness.client.post(
        "/api/v1/contacts", headers={**user, **harness.ik()}, json={"phone": "+12125550172", "first_name": "Old"}
    ).json()
    consent = {"channels": ["phone", "email"], "source": "web_form", "policy_version": "2026-01"}
    r = _upload(harness, user, CSV.encode(), {"consent": consent, "source": "spring-fair"})
    assert r.status_code == 202, r.text
    import_id = r.json()["id"]
    name, queue, ctx, ids = harness.enqueued[-1]
    assert (name, queue, ids) == ("imports.process", "imports.process", {"import_id": import_id})

    async def twice(deps):
        return [
            await process_import(deps, ctx, uuid.UUID(import_id)),
            await process_import(deps, ctx, uuid.UUID(import_id)),
        ]

    assert harness.job(twice) == ["completed", "skipped"]
    done = harness.client.get(f"/api/v1/imports/{import_id}", headers=user).json()
    assert (done["status"], done["total_rows"], done["created_count"], done["updated_count"], done["failed_count"]) == (
        "completed",
        7,
        1,
        2,
        4,
    )
    link = harness.client.get(f"/api/v1/imports/{import_id}/report", headers=user).json()
    key = link["url"].split("memory://imports/", 1)[1].split("?", 1)[0]
    report = harness.store.objects[("imports", key)][0].decode().splitlines()
    assert report[0] == "row,status,error"
    assert report[1:] == [
        "1,created,",
        "2,updated,",
        "3,updated,",
        "4,failed,invalid_email",
        "5,failed,invalid_phone",
        "6,failed,missing_email_or_phone",
        "7,failed,invalid_custom_field",
    ]
    assert "not-an-email" not in "\n".join(report)  # the report never echoes row values
    found = harness.client.get("/api/v1/contacts?q=new1", headers=user).json()["items"]
    assert len(found) == 1 and found[0]["first_name"] == "Nia Updated" and found[0]["custom_fields"] == {"plan": "pro"}
    assert found[0]["phone_e164"] == "+12125550171" and found[0]["source"] == "spring-fair"
    updated = harness.client.get(f"/api/v1/contacts/{existing['id']}", headers=user).json()
    assert updated["first_name"] == "Existing By Phone"
    consents = harness.client.get(f"/api/v1/contacts/{found[0]['id']}/consents", headers=user).json()["items"]
    assert {c["channel"] for c in consents} == {"phone", "email"} and consents[0]["policy_version"] == "2026-01"
    assert harness.run(_scalar(harness, "SELECT count(*) FROM outbox WHERE event_type = 'import.completed'", a)) >= 1


def test_import_upload_limits(harness, tenants):
    a, _ = tenants
    user = harness.user(a)
    assert _upload(harness, user, b"email\nx@example.test\n", name="leads.xlsx").status_code == 422
    assert _upload(harness, user, b"password,email\n", name="x.csv").status_code == 422
    assert _upload(harness, user, b"first_name\nAda\n").status_code == 422  # needs an email or phone column
    assert _upload(harness, user, "email\n\xff".encode("latin-1")).status_code == 422
    settings = harness.app.state.settings
    original, settings.import_max_bytes = settings.import_max_bytes, 64
    try:
        assert _upload(harness, user, b"email\n" + b"a@example.test\n" * 10).status_code == 422
    finally:
        settings.import_max_bytes = original
    viewer = harness.user(a, {"leads:read"})
    assert _upload(harness, viewer, b"email\na@example.test\n").status_code == 403


def test_sweeper_reenqueues_stuck_jobs(harness, tenants):
    a, _ = tenants
    user = harness.user(a)
    r = _upload(harness, user, b"email\nsweep@example.test\n")
    harness.enqueued.clear()
    calls = []

    async def record(name, queue, ctx, **ids):
        calls.append((name, ids))

    async def run(deps):
        fresh = await sweep_stuck_jobs(deps.db, record, stale_seconds=600)
        aged = await sweep_stuck_jobs(deps.db, record, stale_seconds=0)
        return fresh, aged

    fresh, aged = harness.job(run)
    assert fresh == 0 and aged >= 1 and ("imports.process", {"import_id": r.json()["id"]}) in calls


# ------------------------------------------------------------------------------------------- events
class FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body, self.headers, self.acked, self.rejected = body, {}, False, None

    async def ack(self) -> None:
        self.acked = True

    async def reject(self, requeue: bool = False) -> None:
        self.rejected = requeue


def _event(event_type, tenant_id, payload, producer):
    return make_event(
        event_type,
        producer=producer,
        actor=ActorRef(type="service", id=producer),
        correlation_id=uuid.uuid4(),
        tenant_id=tenant_id,
        payload=payload,
    )


def _deliver(harness, *events):
    async def run(deps):
        consumer = EventConsumer(
            consumer_name="crm_service",
            db=deps.db,
            inbox=inbox,
            amqp_url="amqp://unused",
            handlers=HANDLERS,
            allowed_producers=ALLOWED_PRODUCERS,
        )
        out = []
        for e in events:
            msg = FakeMessage(e.model_dump_json().encode())
            await consumer.handle_message(msg)
            out.append((msg.acked, msg.rejected))
        return out

    return harness.job(run)


@pytest.mark.security
def test_opt_out_event_suppresses_once_and_forged_producer_is_ignored(harness, tenants):
    a, b = tenants
    user = harness.user(a)
    c = harness.client.post(
        "/api/v1/contacts", headers={**user, **harness.ik()}, json={"phone": "+12125550181", "email": "oo@example.test"}
    ).json()
    forged = _event("call.opted_out", a, {"call_id": str(uuid.uuid4()), "contact_id": c["id"]}, "voice-service")
    other_tenant = _event("call.opted_out", b, {"call_id": str(uuid.uuid4()), "contact_id": c["id"]}, "call-service")
    real = _event("call.opted_out", a, {"call_id": str(uuid.uuid4()), "contact_id": c["id"]}, "call-service")
    assert _deliver(harness, forged, other_tenant) == [(True, None), (True, None)]
    state = harness.client.get(f"/api/v1/contacts/{c['id']}/contactability", headers=user).json()
    assert state["phone"]["allowed"] is True  # neither the forged nor the cross-tenant event had any effect
    assert _deliver(harness, real, real) == [(True, None), (True, None)]  # redelivery is a no-op
    state = harness.client.get(f"/api/v1/contacts/{c['id']}/contactability", headers=user).json()
    assert state["phone"]["reasons"] == ["suppressed", "no_consent"] and state["email"]["allowed"] is True
    assert harness.run(_scalar(harness, "SELECT count(*) FROM suppressions", a)) == 1
    assert harness.run(_scalar(harness, "SELECT count(*) FROM consent_records", a)) == 1


def test_email_events_and_lead_lifecycle_events(harness, tenants):
    a, _ = tenants
    user = harness.user(a)
    lead = harness.client.post(
        "/api/v1/leads", headers={**user, **harness.ik()}, json={"contact": {"email": "life@example.test"}}
    ).json()
    cid = lead["contact_id"]
    unsub = _event(
        "email.unsubscribed", a, {"address_hash": value_hash("life@example.test"), "contact_id": cid}, "email-service"
    )
    stranger = _event("email.complaint", a, {"address_hash": value_hash("gone@example.test")}, "email-service")
    outcome = _event(
        "call.outcome.extracted",
        a,
        {"call_id": str(uuid.uuid4()), "outcome": "interested", "qualified": True, "contact_id": cid},
        "ai-agent-service",
    )
    booking = _event(
        "booking.created",
        a,
        {"booking_id": str(uuid.uuid4()), "contact_id": cid, "status": "tentative"},
        "booking-service",
    )
    late_outcome = _event(
        "call.outcome.extracted",
        a,
        {"call_id": str(uuid.uuid4()), "outcome": "not_now", "qualified": False, "contact_id": cid},
        "ai-agent-service",
    )
    assert all(acked for acked, _ in _deliver(harness, unsub, stranger, outcome, booking, booking, late_outcome))
    state = harness.client.get(f"/api/v1/contacts/{cid}/contactability", headers=user).json()
    assert state["email"]["reasons"] == ["suppressed", "no_consent"]
    check = harness.client.get(
        "/internal/v1/suppressions/check?channel=email&value=gone@example.test",
        headers=harness.service("svc-email-service", a),
    ).json()
    assert check == {"suppressed": True, "reasons": ["complaint"]}
    after = harness.client.get(f"/api/v1/leads/{lead['id']}", headers=user).json()
    # Booked is not downgraded by a later outcome; the qualification flag still records the latest result.
    assert after["status"] == "booked" and after["qualified"] is False and after["last_outcome"] == "not_now"
    assert after["booking_id"] == booking.payload["booking_id"]


def test_seed_is_idempotent_and_matches_the_demo_contract(harness):
    from crm_service.seed import seed
    from nexus_core.devseed import ACME, GLOBEX, demo_id

    async def run(deps):
        await seed(deps.db)
        await seed(deps.db)
        out = {}
        for t in (ACME, GLOBEX):
            async with deps.db.tenant_session(t.id) as s:
                out[t.slug] = (
                    (await s.execute(text("SELECT count(*) FROM contacts"))).scalar_one(),
                    (await s.execute(text("SELECT count(*) FROM suppressions"))).scalar_one(),
                    set((await s.execute(text("SELECT id FROM segments"))).scalars()),
                )
        return out

    counts = harness.job(run)
    assert counts["acme"][:2] == (40, 3) and counts["globex"][:2] == (40, 3)
    assert counts["acme"][2] == {demo_id("segment", 1)} and counts["globex"][2] == {demo_id("segment", 101)}
    svc = harness.service("svc-campaign-service", ACME.id)
    page = harness.client.post(
        f"/internal/v1/segments/{demo_id('segment', 1)}/resolve", headers=svc, json={"channel": "phone"}
    ).json()
    assert len(page["items"]) == 37  # 40 minus #37 (phone consent withdrawn), #38 and #40 (phone suppressions)
