"""Public API against a real database: CRUD, filters, pagination, idempotency, permissions, tenant isolation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.integration]


def _contact(h, headers, **body):
    body.setdefault("first_name", "Ada")
    r = h.client.post("/api/v1/contacts", headers={**headers, **h.ik()}, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------------------------------ contacts
def test_contact_crud_normalizes_and_versions(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c = _contact(harness, h, email=" Ada@Example.TEST ", phone="(212) 555-0101", last_name="Lovelace")
    assert c["email"] == "ada@example.test" and c["phone_e164"] == "+12125550101" and c["country"] == "US"
    got = harness.client.get(f"/api/v1/contacts/{c['id']}", headers=h).json()
    assert got["version"] == 1
    r = harness.client.patch(
        f"/api/v1/contacts/{c['id']}", headers=h, json={"company": "Analytical Engines", "expected_version": 1}
    )
    assert r.status_code == 200 and r.json()["version"] == 2
    stale = harness.client.patch(f"/api/v1/contacts/{c['id']}", headers=h, json={"company": "X", "expected_version": 1})
    assert stale.status_code == 409
    # Email and phone are tenant-unique.
    dup = harness.client.post("/api/v1/contacts", headers={**h, **harness.ik()}, json={"email": "ADA@example.test"})
    assert dup.status_code == 409 and dup.json()["contact_id"] == c["id"]
    bad = harness.client.post("/api/v1/contacts", headers={**h, **harness.ik()}, json={"phone": "12"})
    assert bad.status_code == 422
    assert harness.client.delete(f"/api/v1/contacts/{c['id']}", headers=h).status_code == 204
    assert harness.client.get(f"/api/v1/contacts/{c['id']}", headers=h).json()["status"] == "archived"
    ids = [x["id"] for x in harness.client.get("/api/v1/contacts", headers=h).json()["items"]]
    assert c["id"] not in ids


def test_contact_list_filters_sort_and_keyset_pagination(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    for i in range(7):
        _contact(harness, h, last_name=f"Zed{i}", company="Acme" if i % 2 else "Globex", email=f"p{i}@example.test")
    page1 = harness.client.get("/api/v1/contacts?limit=3&sort=last_name&direction=asc", headers=h).json()
    page2 = harness.client.get(
        f"/api/v1/contacts?limit=3&sort=last_name&direction=asc&cursor={page1['next_cursor']}", headers=h
    ).json()
    page3 = harness.client.get(
        f"/api/v1/contacts?limit=3&sort=last_name&direction=asc&cursor={page2['next_cursor']}", headers=h
    ).json()
    names = [c["last_name"] for p in (page1, page2, page3) for c in p["items"]]
    assert names == [f"Zed{i}" for i in range(7)] and page3["next_cursor"] is None
    # A cursor is bound to its sort.
    assert harness.client.get(f"/api/v1/contacts?cursor={page1['next_cursor']}", headers=h).status_code == 422
    acme = harness.client.get("/api/v1/contacts?q=acm", headers=h).json()["items"]
    assert len(acme) == 3 and all(c["company"] == "Acme" for c in acme)
    assert harness.client.get("/api/v1/contacts?limit=500", headers=h).status_code == 422


def test_create_requires_and_honours_idempotency_key(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    assert harness.client.post("/api/v1/contacts", headers=h, json={"first_name": "No key"}).status_code == 422
    key = {"Idempotency-Key": f"k-{uuid.uuid4()}"}
    first = harness.client.post("/api/v1/contacts", headers={**h, **key}, json={"email": "idem@example.test"})
    again = harness.client.post("/api/v1/contacts", headers={**h, **key}, json={"email": "idem@example.test"})
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"]
    other = harness.client.post("/api/v1/contacts", headers={**h, **key}, json={"email": "other@example.test"})
    assert other.status_code == 422


def test_custom_fields_validated_against_definitions(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    r = harness.client.post(
        "/api/v1/custom-fields",
        headers=h,
        json={"key": "plan", "label": "Plan", "type": "select", "options": ["basic", "pro"]},
    )
    assert r.status_code == 201
    ok = _contact(harness, h, email="cf@example.test", custom_fields={"plan": "pro"})
    assert ok["custom_fields"] == {"plan": "pro"}
    for bad in ({"plan": "gold"}, {"unknown": 1}):
        resp = harness.client.post(
            "/api/v1/contacts", headers={**h, **harness.ik()}, json={"first_name": "x", "custom_fields": bad}
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------------------------- leads
def test_lead_create_dedupes_contact_and_emits_events(harness, tenants, sdb):
    a, _ = tenants
    h = harness.user(a)
    body = {"contact": {"email": "lead@example.test", "first_name": "Lee"}, "source": "web", "score": 40}
    one = harness.client.post("/api/v1/leads", headers={**h, **harness.ik()}, json=body)
    two = harness.client.post("/api/v1/leads", headers={**h, **harness.ik()}, json=body)
    assert one.status_code == two.status_code == 201
    assert one.json()["contact_id"] == two.json()["contact_id"] and one.json()["id"] != two.json()["id"]
    assert one.json()["contact"]["email"] == "lead@example.test"
    lead = one.json()
    upd = harness.client.patch(
        f"/api/v1/leads/{lead['id']}", headers=h, json={"status": "qualified", "expected_version": 1}
    )
    assert upd.status_code == 200 and upd.json()["status"] == "qualified"
    listed = harness.client.get("/api/v1/leads?status=qualified", headers=h).json()["items"]
    assert [x["id"] for x in listed] == [lead["id"]]
    by_score = harness.client.get("/api/v1/leads?sort=score&direction=desc&limit=1", headers=h).json()
    assert by_score["items"][0]["score"] == 40 and by_score["next_cursor"]

    async def events():
        db = harness.database()
        async with db.global_session() as s:
            rows = (
                (await s.execute(text("SELECT event_type FROM outbox WHERE tenant_id = :t ORDER BY id"), {"t": a}))
                .scalars()
                .all()
            )
        await db.dispose()
        return rows

    types = harness.run(events())
    assert types.count("lead.created") == 2 and "contact.updated" in types and "audit.event" in types
    assert harness.client.delete(f"/api/v1/leads/{lead['id']}", headers=h).status_code == 204
    assert harness.client.get(f"/api/v1/leads/{lead['id']}", headers=h).status_code == 404


# ------------------------------------------------------------------------------- consent/suppression
def test_consent_is_append_only_and_drives_contactability(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c = _contact(harness, h, email="consent@example.test", phone="+12125550111")
    url = f"/api/v1/contacts/{c['id']}"
    assert harness.client.get(f"{url}/contactability", headers=h).json()["phone"] == {"allowed": True, "reasons": []}
    for status in ("granted", "withdrawn"):
        r = harness.client.post(
            f"{url}/consents", headers=h, json={"channel": "phone", "status": status, "source": "web_form"}
        )
        assert r.status_code == 201
    state = harness.client.get(f"{url}/contactability", headers=h).json()
    assert state["phone"]["reasons"] == ["no_consent"] and state["email"]["allowed"] is True
    assert len(harness.client.get(f"{url}/consents", headers=h).json()["items"]) == 2

    async def tamper():
        db = harness.database()
        try:
            async with db.tenant_session(a) as s:
                await s.execute(text("UPDATE consent_records SET status = 'granted'"))
        finally:
            await db.dispose()

    with pytest.raises(Exception, match="append-only|permission denied"):
        harness.run(tamper())


def test_suppression_add_and_step_up_removal(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    c = _contact(harness, h, phone="+12125550112")
    r = harness.client.post("/api/v1/suppressions", headers=h, json={"channel": "phone", "contact_id": c["id"]})
    assert r.status_code == 201 and r.json()["value_masked"].endswith("0112")
    again = harness.client.post("/api/v1/suppressions", headers=h, json={"channel": "phone", "value": "212-555-0112"})
    assert again.json()["id"] == r.json()["id"]  # same normalized value → same suppression
    state = harness.client.get(f"/api/v1/contacts/{c['id']}/contactability", headers=h).json()
    assert state["phone"]["reasons"] == ["suppressed"]
    sid = r.json()["id"]
    no_mfa = harness.client.delete(f"/api/v1/suppressions/{sid}", headers=h)
    assert no_mfa.status_code == 401 and no_mfa.json()["code"] == "step_up_required"
    mfa = harness.user(a, mfa=True)
    assert harness.client.delete(f"/api/v1/suppressions/{sid}", headers=mfa).status_code == 204
    assert harness.client.get(f"/api/v1/contacts/{c['id']}/contactability", headers=h).json()["phone"]["allowed"]


# -------------------------------------------------------------------------------------------- segments
def test_segment_crud_and_preview_counts(harness, tenants):
    a, _ = tenants
    h = harness.user(a)
    tag = harness.client.post("/api/v1/tags", headers=h, json={"name": "vip"}).json()
    c1 = _contact(harness, h, email="s1@example.test", phone="+12125550121", tag_ids=[tag["id"]])
    _contact(harness, h, email="s2@example.test", phone="+12125550122", tag_ids=[tag["id"]])
    _contact(harness, h, email="s3@example.test")
    harness.client.post("/api/v1/suppressions", headers=h, json={"channel": "all", "contact_id": c1["id"]})
    rules = {"op": "and", "rules": [{"field": "tag", "operator": "has", "value": "vip"}]}
    seg = harness.client.post("/api/v1/segments", headers=h, json={"name": "VIPs", "rules": rules})
    assert seg.status_code == 201
    pv = harness.client.get(f"/api/v1/segments/{seg.json()['id']}/preview?channel=phone", headers=h).json()
    assert pv == {"matching": 2, "contactable": 1, "channel": "phone"}
    bad = harness.client.post(
        "/api/v1/segments", headers=h, json={"name": "Bad", "rules": {"op": "and", "rules": [{"field": "x"}]}}
    )
    assert bad.status_code == 422
    adhoc = harness.client.post("/api/v1/segments/preview", headers=h, json={"rules": {"op": "and", "rules": []}})
    assert adhoc.json()["matching"] == 3
    upd = harness.client.patch(
        f"/api/v1/segments/{seg.json()['id']}", headers=h, json={"name": "VIP list", "expected_version": 1}
    )
    assert upd.status_code == 200 and upd.json()["version"] == 2


# ---------------------------------------------------------------------------------------------- exports
def test_export_requires_step_up_and_runs_async(harness, tenants):
    from crm_service.jobs import build_export

    a, _ = tenants
    h = harness.user(a)
    _contact(harness, h, email="exp@example.test", first_name="=CMD()")
    harness.client.post("/api/v1/leads", headers={**h, **harness.ik()}, json={"contact": {"email": "exp@example.test"}})
    denied = harness.client.post("/api/v1/leads/exports", headers={**h, **harness.ik()}, json={})
    assert denied.status_code == 401
    mfa = harness.user(a, mfa=True)
    r = harness.client.post(
        "/api/v1/leads/exports", headers={**mfa, **harness.ik()}, json={"filters": {"status": ["new"]}}
    )
    assert r.status_code == 202 and r.json()["status"] == "queued"
    name, queue, ctx, ids = harness.enqueued[-1]
    assert (name, queue) == ("exports.build", "imports.process") and ctx.tenant_id == a
    export_id = uuid.UUID(ids["export_id"])

    async def twice(deps):  # the second delivery of the same task does nothing
        return [await build_export(deps, ctx, export_id), await build_export(deps, ctx, export_id)]

    assert harness.job(twice) == ["completed", "skipped"]
    done = harness.client.get(f"/api/v1/leads/exports/{ids['export_id']}", headers=mfa).json()
    assert (
        done["status"] == "completed"
        and done["row_count"] == 1
        and done["download_url"].startswith("memory://exports/")
    )
    key = done["download_url"].split("memory://exports/", 1)[1].split("?", 1)[0]
    data = harness.store.objects[("exports", key)][0].decode()
    assert key.startswith(f"{a}/") and "'=CMD()" in data and "exp@example.test" in data


# ------------------------------------------------------------------------------- permissions/isolation
@pytest.mark.security
@pytest.mark.parametrize(
    ("method", "path", "perm"),
    [
        ("get", "/api/v1/contacts", "leads:read"),
        ("post", "/api/v1/contacts", "leads:create"),
        ("get", "/api/v1/leads", "leads:read"),
        ("post", "/api/v1/leads", "leads:create"),
        ("post", "/api/v1/leads/exports", "leads:export"),
        ("get", "/api/v1/suppressions", "leads:update"),
        ("post", "/api/v1/suppressions", "leads:update"),
        ("post", "/api/v1/tags", "leads:update"),
        ("post", "/api/v1/custom-fields", "leads:update"),
        ("get", "/api/v1/segments", "campaigns:read"),
        ("post", "/api/v1/segments", "campaigns:create"),
        ("get", "/api/v1/imports", "leads:create"),
    ],
)
def test_permission_boundaries(harness, tenants, method, path, perm):
    a, _ = tenants
    every = {
        "leads:read",
        "leads:create",
        "leads:update",
        "leads:export",
        "campaigns:read",
        "campaigns:create",
        "campaigns:manage",
    }
    headers = {**harness.user(a, every - {perm}, mfa=True), **harness.ik()}
    kwargs = {"json": {}} if method == "post" else {}
    r = getattr(harness.client, method)(path, headers=headers, **kwargs)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == f"Missing permission: {perm}"
    # Without any membership the tenant itself is refused.
    stranger = harness.tokens.user(str(uuid.uuid4()), "stranger@example.test", mfa=True)
    r = getattr(harness.client, method)(
        path, headers={"Authorization": f"Bearer {stranger}", "X-Tenant-ID": str(a)}, **kwargs
    )
    assert r.status_code == 403


@pytest.mark.security
def test_cross_tenant_access_is_not_possible(harness, tenants):
    a, b = tenants
    ha, hb = harness.user(a), harness.user(b)
    c = _contact(harness, ha, email="iso@example.test")
    lead = harness.client.post("/api/v1/leads", headers={**ha, **harness.ik()}, json={"contact_id": c["id"]}).json()
    seg = harness.client.post(
        "/api/v1/segments", headers=ha, json={"name": "A only", "rules": {"op": "and", "rules": []}}
    ).json()
    supp = harness.client.post(
        "/api/v1/suppressions", headers=ha, json={"channel": "email", "contact_id": c["id"]}
    ).json()
    mfa_b = harness.user(b, mfa=True)
    probes = [
        ("get", f"/api/v1/contacts/{c['id']}", None, hb),
        ("patch", f"/api/v1/contacts/{c['id']}", {"company": "x", "expected_version": 1}, hb),
        ("delete", f"/api/v1/contacts/{c['id']}", None, hb),
        ("get", f"/api/v1/contacts/{c['id']}/consents", None, hb),
        ("post", f"/api/v1/contacts/{c['id']}/notes", {"text": "hi"}, hb),
        ("get", f"/api/v1/leads/{lead['id']}", None, hb),
        ("patch", f"/api/v1/leads/{lead['id']}", {"score": 1, "expected_version": 1}, hb),
        ("delete", f"/api/v1/leads/{lead['id']}", None, hb),
        ("get", f"/api/v1/segments/{seg['id']}", None, hb),
        ("delete", f"/api/v1/segments/{seg['id']}", None, hb),
        ("delete", f"/api/v1/suppressions/{supp['id']}", None, mfa_b),
    ]
    for method, path, body, headers in probes:
        kwargs = {"json": body} if body is not None else {}
        r = getattr(harness.client, method)(path, headers=headers, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)
    assert harness.client.get("/api/v1/contacts", headers=hb).json()["items"] == []
    # Tenant B's export contains nothing of tenant A.
    r = harness.client.post("/api/v1/leads/exports", headers={**mfa_b, **harness.ik()}, json={})
    from crm_service.jobs import build_export

    _, _, ctx, ids = harness.enqueued[-1]
    harness.job(lambda deps: build_export(deps, ctx, uuid.UUID(ids["export_id"])))
    assert harness.client.get(f"/api/v1/leads/exports/{ids['export_id']}", headers=mfa_b).json()["row_count"] == 0
    assert harness.client.get(f"/api/v1/leads/exports/{ids['export_id']}", headers=ha).status_code in (403, 404)
    # A member of A asking for B's tenant is refused outright.
    assert harness.client.get("/api/v1/contacts", headers={**ha, "X-Tenant-ID": str(b)}).status_code == 403
