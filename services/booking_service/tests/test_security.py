"""Tenant isolation and permission boundaries for every booking resource (build.md §5.3, §15)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from nexus_core.testing import auth

from .conftest import MANAGE, READ, iso, slot_start

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _booking(h, tenant, user, rid, hour=10, key=None):
    start = slot_start(6, hour)
    r = h.client.post(
        "/api/v1/bookings",
        headers=auth(user, tenant, **{"Idempotency-Key": key or uuid.uuid4().hex}),
        json={"contact_id": str(uuid.uuid4()), "start": iso(start), "end": iso(start + timedelta(minutes=30)),
              "resource_id": rid},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()


def test_cross_tenant_read_update_delete_are_denied(harness):
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    rid_a = harness.resource(tenant_a)
    harness.resource(tenant_b)
    user_a = harness.user(tenant_a, MANAGE)
    user_b = harness.user(tenant_b, MANAGE)
    bk = _booking(harness, tenant_a, user_a, rid_a)
    hb = auth(user_b, tenant_b)

    # Reads: the other tenant's booking does not exist for B.
    assert harness.client.get(f"/api/v1/bookings/{bk['id']}", headers=hb).status_code == 404
    assert harness.client.get("/api/v1/bookings", headers=hb).json()["items"] == []
    cal = harness.client.get("/api/v1/bookings/calendar", headers=hb,
                             params={"from": iso(slot_start(5, 0)), "to": iso(slot_start(8, 0))})  # fmt: skip
    assert cal.json()["items"] == []
    assert rid_a not in {r["id"] for r in harness.client.get("/api/v1/bookings/resources", headers=hb).json()["items"]}
    # Updates / cancel ("delete") / reschedule / rules: all 404 for B.
    key = {"Idempotency-Key": "x"}
    assert harness.client.post(f"/api/v1/bookings/{bk['id']}/cancel", headers={**hb, **key},
                               json={"reason": "requested"}).status_code == 404  # fmt: skip
    assert harness.client.post(f"/api/v1/bookings/{bk['id']}/reschedule", headers={**hb, **key},
                               json={"start": iso(slot_start(6, 12)), "end": iso(slot_start(6, 12, 30)),
                                     "expected_version": 1}).status_code == 404  # fmt: skip
    assert harness.client.post(f"/api/v1/bookings/{bk['id']}/outcome", headers=hb,
                               json={"status": "no_show", "expected_version": 1}).status_code == 404  # fmt: skip
    assert harness.client.patch(f"/api/v1/bookings/resources/{rid_a}", headers=hb,
                                json={"name": "hijack", "expected_version": 2}).status_code == 404  # fmt: skip
    assert harness.client.put(f"/api/v1/bookings/resources/{rid_a}/rules", headers=hb,
                              json={"rules": [], "expected_version": 2}).status_code == 404  # fmt: skip
    assert harness.client.get(f"/api/v1/bookings/resources/{rid_a}/rules", headers=hb).status_code == 404
    # B cannot book A's resource either.
    start = slot_start(6, 14)
    r = harness.client.post("/api/v1/bookings", headers={**hb, "Idempotency-Key": "y"},
                            json={"contact_id": str(uuid.uuid4()), "start": iso(start),
                                  "end": iso(start + timedelta(minutes=30)), "resource_id": rid_a})  # fmt: skip
    assert r.status_code == 404
    # Internal API with B's tenant header cannot see or confirm A's booking.
    assert harness.client.post(f"/internal/v1/bookings/{bk['id']}/confirm", headers=harness.internal(tenant_b, "z"),
                               json={"contact_id": bk["contact_id"]}).status_code == 404  # fmt: skip
    # A user of A cannot select tenant B (no membership).
    assert harness.client.get("/api/v1/bookings", headers=auth(user_a, tenant_b)).status_code == 403
    # A is unaffected.
    assert harness.client.get(f"/api/v1/bookings/{bk['id']}", headers=auth(user_a, tenant_a)).json()["version"] == 1


def test_rls_hides_other_tenants_rows_even_with_raw_sql(harness):
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    rid = harness.resource(tenant_a)
    _booking(harness, tenant_a, harness.user(tenant_a, MANAGE), rid)
    for table in ("appointments", "resources", "availability_rules"):
        assert harness.sql(f"SELECT count(*) AS n FROM {table}", tenant_id=tenant_b)[0]["n"] == 0
        assert harness.sql(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0
    with pytest.raises(Exception, match="row-level security"):
        harness.sql("INSERT INTO resources (tenant_id, name) VALUES (:t, 'evil')", {"t": tenant_a}, tenant_id=tenant_b)


def test_permission_boundaries(harness, tenant):
    rid = harness.resource(tenant)
    reader = harness.user(tenant, READ)
    nobody = harness.user(tenant, {"leads:read"})
    manager = harness.user(tenant, MANAGE)
    bk = _booking(harness, tenant, manager, rid)
    hr, hn = auth(reader, tenant), auth(nobody, tenant)
    key = {"Idempotency-Key": "k"}
    start = slot_start(6, 15)
    create = {"contact_id": str(uuid.uuid4()), "start": iso(start), "end": iso(start + timedelta(minutes=30))}
    # bookings:read can read but not change anything.
    assert harness.client.get("/api/v1/bookings", headers=hr).status_code == 200
    assert harness.client.get("/api/v1/availability", headers=hr,
                              params={"from": iso(slot_start(6, 0)), "to": iso(slot_start(7, 0))}).status_code == 200  # fmt: skip
    for method, path, body in (
        ("post", "/api/v1/bookings", create),
        ("post", f"/api/v1/bookings/{bk['id']}/cancel", {"reason": "requested"}),
        ("post", f"/api/v1/bookings/{bk['id']}/reschedule", {**{k: create[k] for k in ("start", "end")},
                                                             "expected_version": 1}),
        ("post", f"/api/v1/bookings/{bk['id']}/outcome", {"status": "no_show", "expected_version": 1}),
        ("post", "/api/v1/bookings/resources", {"name": "New"}),
        ("patch", f"/api/v1/bookings/resources/{rid}", {"name": "New", "expected_version": 2}),
        ("put", f"/api/v1/bookings/resources/{rid}/rules", {"rules": [], "expected_version": 2}),
    ):  # fmt: skip
        assert getattr(harness.client, method)(path, headers={**hr, **key}, json=body).status_code == 403, path
    # Without bookings:read nothing is visible.
    for path in ("/api/v1/bookings", f"/api/v1/bookings/{bk['id']}", "/api/v1/bookings/resources"):
        assert harness.client.get(path, headers=hn).status_code == 403, path
    # No token at all.
    assert harness.client.get("/api/v1/bookings").status_code == 401


def test_internal_api_accepts_only_allowlisted_services(harness, tenant):
    harness.resource(tenant)
    params = {"from": iso(slot_start(3, 0)), "to": iso(slot_start(4, 0))}
    user = harness.user(tenant, MANAGE)
    assert harness.client.get("/internal/v1/availability", params=params, headers=auth(user, tenant)).status_code == 403
    rogue = harness.internal(tenant, client_id="svc-crm-service")
    assert harness.client.get("/internal/v1/availability", params=params, headers=rogue).status_code == 403
    no_tenant = auth(harness.service())
    assert harness.client.get("/internal/v1/availability", params=params, headers=no_tenant).status_code == 403
    ok = harness.client.get("/internal/v1/availability", params=params, headers=harness.internal(tenant))
    assert ok.status_code == 200 and len(ok.json()["slots"]) == 20
    assert set(ok.json()) == {"slots"} and set(ok.json()["slots"][0]) == {"start", "end", "resource_id"}
    too_long = {"from": iso(slot_start(3, 0)), "to": iso(slot_start(40, 0))}
    assert harness.client.get("/internal/v1/availability", params=too_long,
                              headers=harness.internal(tenant)).status_code == 422  # fmt: skip


def test_tenant_header_is_ignored_in_bodies(harness, tenant):
    """Requests cannot smuggle a tenant or extra fields (extra=forbid)."""
    harness.resource(tenant)
    start = slot_start(3, 10)
    body = {"contact_id": str(uuid.uuid4()), "start": iso(start), "end": iso(start + timedelta(minutes=30)),
            "source": {"type": "ai_tool", "id": "e"}, "tenant_id": str(uuid.uuid4())}  # fmt: skip
    r = harness.client.post("/internal/v1/bookings/tentative", headers=harness.internal(tenant, "t"), json=body)
    assert r.status_code == 422
