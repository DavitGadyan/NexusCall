"""Booking behaviour against a real database: double-booking guard, idempotency, ownership, expiry,
reschedule conflicts, events, reminders (build.md §3.1J, §5.4)."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from booking_service.service import expire_holds, send_reminders
from nexus_core.testing import auth

from .conftest import MANAGE, READ, iso, slot_start

pytestmark = [pytest.mark.integration]


def _tentative(h, tenant, *, contact=None, start=None, key=None, resource=None, minutes=30):
    start = start or slot_start()
    body = {
        "contact_id": str(contact or uuid.uuid4()),
        "start": iso(start),
        "end": iso(start + timedelta(minutes=minutes)),
        "source": {"type": "ai_tool", "id": str(uuid.uuid4())},
    }
    if resource:
        body["resource_id"] = resource
    return h.client.post(
        "/internal/v1/bookings/tentative", headers=h.internal(tenant, key or str(uuid.uuid4())), json=body
    ), body


# ------------------------------------------------------------------------------------------- double booking
def test_concurrent_tentative_bookings_for_one_slot_exactly_one_succeeds(harness, tenant):
    harness.resource(tenant)
    start = slot_start(4, 11)

    def attempt(_: int) -> int:
        resp, _ = _tentative(harness, tenant, start=start)
        return resp.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(attempt, range(8)))
    assert sorted(codes) == [201] + [409] * 7, codes
    rows = harness.sql("SELECT count(*) AS n FROM appointments WHERE status = 'tentative'", tenant_id=tenant)
    assert rows[0]["n"] == 1


def test_database_constraint_rejects_overlap_even_without_service_checks(harness, tenant):
    from sqlalchemy.exc import IntegrityError

    rid = harness.resource(tenant)
    start = slot_start(5, 9)
    sql = (
        "INSERT INTO appointments (tenant_id, resource_id, contact_id, start_at, end_at, status, source_type, "
        "created_by) VALUES (:t, :r, :c, :s, :e, 'confirmed', 'api', 'test')"
    )
    params = {"t": tenant, "r": rid, "c": uuid.uuid4(), "s": start, "e": start + timedelta(minutes=30)}
    harness.sql(sql, params, tenant_id=tenant)
    with pytest.raises(IntegrityError):
        harness.sql(sql, {**params, "s": start + timedelta(minutes=15), "e": start + timedelta(minutes=45)},
                    tenant_id=tenant)  # fmt: skip
    # Canceled rows never block.
    harness.sql("UPDATE appointments SET status = 'canceled' WHERE tenant_id = :t", {"t": tenant}, tenant_id=tenant)
    harness.sql(sql, params, tenant_id=tenant)


def test_without_resource_id_the_next_free_resource_is_used(harness, tenant):
    r1, r2 = harness.resource(tenant), harness.resource(tenant)
    start = slot_start(3, 13)
    a, _ = _tentative(harness, tenant, start=start)
    b, _ = _tentative(harness, tenant, start=start)
    c, _ = _tentative(harness, tenant, start=start)
    assert a.status_code == b.status_code == 201 and c.status_code == 409
    used = {row["resource_id"] for row in harness.sql("SELECT resource_id FROM appointments", tenant_id=tenant)}
    assert {str(u) for u in used} == {r1, r2}


# ------------------------------------------------------------------------------------------- idempotency
def test_idempotent_tentative_replay_and_payload_mismatch(harness, tenant):
    harness.resource(tenant)
    key = "tool:" + uuid.uuid4().hex
    first, body = _tentative(harness, tenant, key=key)
    assert first.status_code == 201
    replay = harness.client.post("/internal/v1/bookings/tentative", headers=harness.internal(tenant, key), json=body)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert harness.sql("SELECT count(*) AS n FROM appointments", tenant_id=tenant)[0]["n"] == 1
    other = {**body, "contact_id": str(uuid.uuid4())}
    mismatch = harness.client.post("/internal/v1/bookings/tentative", headers=harness.internal(tenant, key),
                                   json=other)  # fmt: skip
    assert mismatch.status_code == 422
    missing = harness.client.post("/internal/v1/bookings/tentative", headers=harness.internal(tenant), json=body)
    assert missing.status_code == 422


def test_public_create_is_idempotent_and_audited(harness, tenant):
    rid = harness.resource(tenant)
    user = harness.user(tenant, MANAGE)
    start = slot_start(6, 15)
    body = {"contact_id": str(uuid.uuid4()), "start": iso(start), "end": iso(start + timedelta(minutes=45)),
            "resource_id": rid}  # fmt: skip
    headers = auth(user, tenant, **{"Idempotency-Key": "ui-create-1"})
    a = harness.client.post("/api/v1/bookings", headers=headers, json=body)
    b = harness.client.post("/api/v1/bookings", headers=headers, json=body)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert a.json()["status"] == "confirmed"
    audits = [e for e in harness.outbox("audit.event") if e["payload"]["resource_id"] == a.json()["id"]]
    assert [e["payload"]["action"] for e in audits] == ["booking.created"]
    assert audits[0]["actor"]["type"] == "user"


# ------------------------------------------------------------------------------------------- ownership
def test_confirm_requires_the_booking_contact(harness, tenant):
    harness.resource(tenant)
    contact = uuid.uuid4()
    created, _ = _tentative(harness, tenant, contact=contact)
    booking_id = created.json()["booking_id"]
    url = f"/internal/v1/bookings/{booking_id}/confirm"
    source = {"type": "ai_tool", "id": "exec-1"}
    wrong = harness.client.post(url, headers=harness.internal(tenant, "c1"),
                                json={"contact_id": str(uuid.uuid4()), "source": source})  # fmt: skip
    assert wrong.status_code == 404
    unknown = harness.client.post(f"/internal/v1/bookings/{uuid.uuid4()}/confirm", headers=harness.internal(tenant, "c2"),
                                  json={"contact_id": str(contact), "source": source})  # fmt: skip
    assert unknown.status_code == 404 and unknown.json()["detail"] == wrong.json()["detail"]
    no_contact = harness.client.post(url, headers=harness.internal(tenant, "c3"), json={"source": source})
    assert no_contact.status_code == 422
    ok = harness.client.post(url, headers=harness.internal(tenant, "c4"), json={"contact_id": str(contact),
                                                                                  "source": source})  # fmt: skip
    assert ok.status_code == 200 and ok.json() == {"booking_id": booking_id, "status": "confirmed"}
    again = harness.client.post(url, headers=harness.internal(tenant, "c4"), json={"contact_id": str(contact),
                                                                                     "source": source})  # fmt: skip
    assert again.json() == ok.json()
    assert len([e for e in harness.outbox("booking.confirmed") if e["payload"]["booking_id"] == booking_id]) == 1


def test_internal_cancel_checks_contact_and_never_stores_free_text(harness, tenant):
    harness.resource(tenant)
    contact = uuid.uuid4()
    booking_id = _tentative(harness, tenant, contact=contact)[0].json()["booking_id"]
    url = f"/internal/v1/bookings/{booking_id}/cancel"
    bad = harness.client.post(url, headers=harness.internal(tenant, "x1"),
                              json={"reason": "caller asked", "contact_id": str(uuid.uuid4())})  # fmt: skip
    assert bad.status_code == 404
    ok = harness.client.post(url, headers=harness.internal(tenant, "x2"),
                             json={"reason": "Call me at +1 555 0100", "contact_id": str(contact)})  # fmt: skip
    assert ok.status_code == 200 and ok.json()["status"] == "canceled"
    row = harness.sql("SELECT cancel_reason FROM appointments WHERE id = :i", {"i": booking_id}, tenant_id=tenant)[0]
    assert row["cancel_reason"] == "other"
    event = next(e for e in harness.outbox("booking.canceled") if e["payload"]["booking_id"] == booking_id)
    assert event["payload"] == {"booking_id": booking_id, "contact_id": str(contact), "status": "canceled"}
    assert event["producer"] == "booking-service"


def test_tentative_must_match_a_bookable_slot(harness, tenant):
    harness.resource(tenant)
    off_grid, _ = _tentative(harness, tenant, start=slot_start(3, 10, 10))
    after_hours, _ = _tentative(harness, tenant, start=slot_start(3, 20))
    past, _ = _tentative(harness, tenant, start=slot_start(-1, 10))
    assert off_grid.status_code == after_hours.status_code == past.status_code == 422


# ------------------------------------------------------------------------------------------- expiry
def test_tentative_holds_expire_and_free_the_slot(harness, tenant):
    harness.resource(tenant)
    contact = uuid.uuid4()
    start = slot_start(3, 14)
    booking_id = _tentative(harness, tenant, contact=contact, start=start)[0].json()["booking_id"]
    # The slot is gone from availability while held.
    params = {"from": iso(start), "to": iso(start + timedelta(minutes=30))}
    assert harness.client.get("/internal/v1/availability", params=params,
                              headers=harness.internal(tenant)).json()["slots"] == []  # fmt: skip
    harness.sql("UPDATE appointments SET hold_expires_at = now() - interval '1 minute' WHERE id = :i",
                {"i": booking_id}, tenant_id=tenant)  # fmt: skip
    # An expired hold cannot be confirmed.
    late = harness.client.post(f"/internal/v1/bookings/{booking_id}/confirm", headers=harness.internal(tenant, "k"),
                               json={"contact_id": str(contact)})  # fmt: skip
    assert late.status_code == 409
    assert harness.run(expire_holds(harness.app)) >= 1
    row = harness.sql("SELECT status, cancel_reason FROM appointments WHERE id = :i", {"i": booking_id},
                      tenant_id=tenant)[0]  # fmt: skip
    assert (row["status"], row["cancel_reason"]) == ("canceled", "hold_expired")
    assert any(e["payload"]["booking_id"] == booking_id for e in harness.outbox("booking.canceled"))
    assert harness.run(expire_holds(harness.app)) == 0  # idempotent
    slots = harness.client.get("/internal/v1/availability", params=params, headers=harness.internal(tenant))
    assert len(slots.json()["slots"]) == 1
    assert _tentative(harness, tenant, start=start)[0].status_code == 201


def test_an_expired_but_unswept_hold_is_released_by_the_next_booking(harness, tenant):
    harness.resource(tenant)
    start = slot_start(3, 15)
    first = _tentative(harness, tenant, start=start)[0].json()["booking_id"]
    harness.sql("UPDATE appointments SET hold_expires_at = now() - interval '1 second' WHERE id = :i", {"i": first},
                tenant_id=tenant)  # fmt: skip
    assert _tentative(harness, tenant, start=start)[0].status_code == 201
    status = harness.sql("SELECT status FROM appointments WHERE id = :i", {"i": first}, tenant_id=tenant)[0]["status"]
    assert status == "canceled"


# ------------------------------------------------------------------------------------------- reschedule
def _public_booking(h, tenant, user, rid, start, key):
    r = h.client.post(
        "/api/v1/bookings",
        headers=auth(user, tenant, **{"Idempotency-Key": key}),
        json={"contact_id": str(uuid.uuid4()), "start": iso(start), "end": iso(start + timedelta(minutes=30)),
              "resource_id": rid},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()


def test_reschedule_conflicts_and_optimistic_locking(harness, tenant):
    rid = harness.resource(tenant)
    user = harness.user(tenant, MANAGE)
    a = _public_booking(harness, tenant, user, rid, slot_start(4, 9), "a")
    b = _public_booking(harness, tenant, user, rid, slot_start(4, 10), "b")
    url = f"/api/v1/bookings/{b['id']}/reschedule"

    def move(start, version, key):
        return harness.client.post(url, headers=auth(user, tenant, **{"Idempotency-Key": key}),
                                   json={"start": iso(start), "end": iso(start + timedelta(minutes=30)),
                                         "expected_version": version})  # fmt: skip

    assert move(slot_start(4, 9, 15), b["version"], "m1").status_code == 409  # overlaps a
    assert move(slot_start(4, 12), b["version"] + 5, "m2").status_code == 409  # stale version
    assert move(slot_start(4, 23), b["version"], "m3").status_code == 422  # outside hours
    ok = move(slot_start(4, 12), b["version"], "m4")
    assert ok.status_code == 200 and ok.json()["version"] == b["version"] + 1
    assert ok.json()["start"].startswith(slot_start(4, 12).isoformat()[:16])
    assert move(slot_start(4, 12), b["version"], "m4").json() == ok.json()  # replay
    # The old slot is free again; `a` is untouched.
    assert move(slot_start(4, 10), ok.json()["version"], "m5").status_code == 200
    assert harness.client.get(f"/api/v1/bookings/{a['id']}", headers=auth(user, tenant)).json()["version"] == 1


def test_cancel_and_outcome_follow_the_state_machine(harness, tenant):
    rid = harness.resource(tenant)
    user = harness.user(tenant, MANAGE)
    bk = _public_booking(harness, tenant, user, rid, slot_start(5, 11), "s1")
    early = harness.client.post(f"/api/v1/bookings/{bk['id']}/outcome", headers=auth(user, tenant),
                                json={"status": "completed", "expected_version": bk["version"]})  # fmt: skip
    assert early.status_code == 409  # not started yet
    c = harness.client.post(f"/api/v1/bookings/{bk['id']}/cancel", headers=auth(user, tenant, **{"Idempotency-Key": "c"}),
                            json={"reason": "no_longer_needed"})  # fmt: skip
    assert c.status_code == 200 and c.json()["status"] == "canceled"
    again = harness.client.post(f"/api/v1/bookings/{bk['id']}/reschedule",
                                headers=auth(user, tenant, **{"Idempotency-Key": "r"}),
                                json={"start": iso(slot_start(5, 12)), "end": iso(slot_start(5, 12, 30)),
                                      "expected_version": c.json()["version"]})  # fmt: skip
    assert again.status_code == 409


# ------------------------------------------------------------------------------------------- listing
def test_list_pagination_and_calendar_range(harness, tenant):
    rid = harness.resource(tenant)
    user = harness.user(tenant, MANAGE)
    for i in range(3):
        _public_booking(harness, tenant, user, rid, slot_start(7, 9 + i), f"l{i}")
    page1 = harness.client.get("/api/v1/bookings", params={"limit": 2}, headers=auth(user, tenant)).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = harness.client.get("/api/v1/bookings", params={"limit": 2, "cursor": page1["next_cursor"]},
                               headers=auth(user, tenant)).json()  # fmt: skip
    assert len(page2["items"]) == 1 and page2["next_cursor"] is None
    cal = harness.client.get("/api/v1/bookings/calendar", headers=auth(user, tenant),
                             params={"from": iso(slot_start(7, 0)), "to": iso(slot_start(8, 0))}).json()  # fmt: skip
    assert [i["start"][11:13] for i in cal["items"]] == ["09", "10", "11"]


# ------------------------------------------------------------------------------------------- reminders
def test_reminder_is_requested_once_for_confirmed_bookings(harness, tenant):
    rid = harness.resource(tenant)
    user = harness.user(tenant, MANAGE)
    bk = _public_booking(harness, tenant, user, rid, slot_start(9, 9), "rem")
    far = _public_booking(harness, tenant, user, rid, slot_start(9, 10), "far")
    # Move one booking inside the 24 h reminder window (the other stays 9 days out).
    harness.sql("UPDATE appointments SET start_at = now() + interval '2 hours', end_at = now() + interval '150 minutes'"
                " WHERE id = :i", {"i": bk["id"]}, tenant_id=tenant)  # fmt: skip
    assert harness.run(send_reminders(harness.app)) >= 1
    assert harness.run(send_reminders(harness.app)) == 0
    events = [e for e in harness.outbox("notification.requested") if e["payload"]["resource_id"] == bk["id"]]
    assert len(events) == 1
    assert not [e for e in harness.outbox("notification.requested") if e["payload"]["resource_id"] == far["id"]]
    assert events[0]["payload"]["template"] == "booking_reminder" and events[0]["tenant_id"] == str(tenant)


def test_seed_is_idempotent(harness):
    from booking_service.seed import seed
    from nexus_core.devseed import DEMO_TENANTS

    harness.run(seed(harness.app.state.db))
    harness.run(seed(harness.app.state.db))
    for t in DEMO_TENANTS:
        rows = harness.sql("SELECT count(*) AS n FROM availability_rules", tenant_id=t.id)
        assert rows[0]["n"] == 5
        user = harness.user(t.id, READ)
        res = harness.client.get("/api/v1/bookings/resources", headers=auth(user, t.id)).json()["items"]
        assert [r["name"] for r in res] == ["Main calendar"] and res[0]["timezone"] == t.timezone
