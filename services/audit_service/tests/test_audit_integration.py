"""Audit store behaviour and security boundaries against a real database (build.md §3.1P, §15, §20.3)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from audit_service.consumer import handle_audit_event
from audit_service.tables import audit_events
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.messaging import PermanentEventError
from nexus_core.testing import auth
from nexus_events import ActorRef, make_event

pytestmark = [pytest.mark.integration, pytest.mark.security]
CONSUMER = Actor("service", "audit_service")


def run_db(url, fn):
    async def go():
        db = Database(url, pool_size=1, max_overflow=0)
        try:
            return await fn(db)
        finally:
            await db.dispose()

    return asyncio.run(go())


def _event(tenant_id, action="lead.exported", *, producer="crm-service", metadata=None, outcome="success"):
    return make_event(
        "audit.event",
        producer=producer,
        actor=ActorRef(type="user", id=str(uuid.uuid4())),
        correlation_id=uuid.uuid4(),
        tenant_id=tenant_id,
        payload={
            "action": action,
            "resource_type": "lead",
            "resource_id": str(uuid.uuid4()),
            "outcome": outcome,
            "request_id": "req-12345678",
            "metadata": metadata or {"count": 3},
        },
    )


def consume(url, *events):
    async def go(db):
        for ev in events:
            ctx = db.tenant_session(ev.tenant_id, CONSUMER) if ev.tenant_id else db.global_session(CONSUMER)
            async with ctx as s:
                await handle_audit_event(ev, s)

    run_db(url, go)


def _chain_rows(url, chain):
    async def go(db):
        async with db.global_session() as s:
            return (
                (
                    await s.execute(
                        select(audit_events).where(audit_events.c.chain == chain).order_by(audit_events.c.chain_seq)
                    )
                )
                .mappings()
                .all()
            )

    return run_db(url, go)


# --------------------------------------------------------------------------- consumer + chain
def test_consumer_appends_chained_rows_and_duplicates_are_idempotent(sdb):
    tenant = uuid.uuid4()
    e1, e2 = _event(tenant), _event(tenant, "lead.deleted", metadata={"password": "x", "n": 1})
    consume(sdb.app_url, e1, e1, e2, e2)
    rows = _chain_rows(sdb.url("platform"), str(tenant))
    assert [r["chain_seq"] for r in rows] == [1, 2]
    assert rows[0]["prev_hash"] == "0" * 64 and rows[1]["prev_hash"] == rows[0]["hash"]
    assert rows[1]["metadata"] == {"n": 1}  # sensitive key stripped before storage
    assert rows[0]["producer"] == "crm-service" and rows[0]["request_id"] == "req-12345678"


def test_platform_events_form_their_own_chain_invisible_to_tenants(sdb):
    tenant = uuid.uuid4()
    consume(sdb.app_url, _event(None, "platform.tenant.created", producer="tenant-service"), _event(tenant))

    async def probe(db):
        async with db.tenant_session(tenant) as s:
            tenants_seen = set((await s.execute(text("SELECT DISTINCT chain FROM audit_events"))).scalars())
        async with db.global_session() as s:
            platform_seen = set((await s.execute(text("SELECT DISTINCT chain FROM audit_events"))).scalars())
        return tenants_seen, platform_seen

    tenants_seen, platform_seen = run_db(sdb.app_url, probe)
    assert tenants_seen == {str(tenant)}
    assert platform_seen == {"platform"}


def test_invalid_payload_is_a_permanent_failure(sdb):
    bad = make_event(
        "audit.event",
        producer="crm-service",
        actor=ActorRef(type="system", id="x"),
        correlation_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        payload={"action": "a", "resource_type": "b"},
    ).model_copy(update={"payload": {"action": "x"}})
    with pytest.raises(PermanentEventError):
        consume(sdb.app_url, bad)


def test_tenant_context_cannot_write_into_another_tenants_chain(sdb):
    ev = _event(uuid.uuid4())

    async def go(db):
        async with db.tenant_session(uuid.uuid4(), CONSUMER) as s:  # wrong tenant context
            await handle_audit_event(ev, s)

    with pytest.raises(DBAPIError):
        run_db(sdb.app_url, go)


def test_concurrent_appends_keep_the_chain_gap_free(sdb):
    tenant = uuid.uuid4()
    events = [_event(tenant, f"x.a{i}") for i in range(12)]

    async def go(db):
        async def one(ev):
            async with db.tenant_session(tenant, CONSUMER) as s:
                await handle_audit_event(ev, s)

        await asyncio.gather(*(one(e) for e in events))

    async def runner():
        db = Database(sdb.app_url, pool_size=6, max_overflow=0)
        try:
            await go(db)
        finally:
            await db.dispose()

    asyncio.run(runner())
    rows = _chain_rows(sdb.url("platform"), str(tenant))
    assert [r["chain_seq"] for r in rows] == list(range(1, 13))
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, 12))


# --------------------------------------------------------------------------- immutability
def test_update_and_delete_are_blocked_for_app_owner_and_superuser(sdb, superuser_url):
    tenant = uuid.uuid4()
    consume(sdb.app_url, _event(tenant))
    row_id = _chain_rows(sdb.url("platform"), str(tenant))[0]["id"]

    async def as_app(db):
        for stmt in ("UPDATE audit_events SET action = 'x'", "DELETE FROM audit_events"):
            with pytest.raises(DBAPIError, match="permission denied"):
                async with db.tenant_session(tenant) as s:
                    await s.execute(text(stmt))

    run_db(sdb.app_url, as_app)

    async def as_raw(dsn: str, expect_trigger: bool):
        conn = await asyncpg.connect(dsn.replace("+asyncpg", ""))
        try:
            await conn.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant))
            for stmt in (
                "UPDATE audit_events SET action = 'tampered' WHERE id = $1",
                "DELETE FROM audit_events WHERE id = $1",
            ):
                if expect_trigger:
                    with pytest.raises(asyncpg.PostgresError, match="append-only"):
                        await conn.execute(stmt, row_id)
                else:
                    # Owner is subject to FORCE RLS and has no UPDATE/DELETE policy: nothing is touched.
                    assert await conn.execute(stmt, row_id) in ("UPDATE 0", "DELETE 0")
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await conn.execute("TRUNCATE audit_events")
        finally:
            await conn.close()

    asyncio.run(as_raw(sdb.owner_url, expect_trigger=False))
    asyncio.run(as_raw(superuser_url, expect_trigger=True))  # superuser bypasses RLS; the trigger still fires
    assert _chain_rows(sdb.url("platform"), str(tenant))[0]["action"] == "lead.exported"


def test_platform_role_is_read_only(sdb):
    async def go(db):
        async with db.global_session() as s:
            await s.execute(
                text(
                    "INSERT INTO audit_events (event_id, chain, chain_seq, prev_hash, hash, occurred_at, actor_type,"
                    " actor_id, action, resource_type, outcome, producer) VALUES (gen_random_uuid(), 'platform', 999,"
                    " 'x', 'y', now(), 'system', 's', 'a', 'r', 'success', 'p')"
                )
            )

    with pytest.raises(DBAPIError, match="permission denied"):
        run_db(sdb.url("platform"), go)


# --------------------------------------------------------------------------- verification
def test_verify_detects_tampering_in_the_database(harness, sdb, superuser_url):
    tenant = uuid.uuid4()
    consume(sdb.app_url, *[_event(tenant, f"lead.a{i}") for i in range(4)])
    _, op = harness.platform_user({"platform:audit:read"})
    ok = harness.client.post("/api/v1/platform/audit/verify", headers=auth(op), json={"tenant_id": str(tenant)})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"chain": str(tenant), "ok": True, "checked": 4, "head_seq": 4, "first_broken": None}

    async def tamper():
        conn = await asyncpg.connect(superuser_url.replace("+asyncpg", ""))
        try:
            await conn.execute("SET session_replication_role = replica")  # disables the append-only trigger
            await conn.execute(
                "UPDATE audit_events SET metadata = '{\"count\": 0}' WHERE chain = $1 AND chain_seq = 3", str(tenant)
            )
        finally:
            await conn.close()

    asyncio.run(tamper())
    bad = harness.client.post("/api/v1/platform/audit/verify", headers=auth(op), json={"tenant_id": str(tenant)})
    body = bad.json()
    assert body["ok"] is False and body["checked"] == 2
    assert body["first_broken"]["chain_seq"] == 3 and body["first_broken"]["reason"] == "hash_mismatch"
    platform = harness.client.post("/api/v1/platform/audit/verify", headers=auth(op), json={})
    assert platform.status_code == 200 and platform.json()["chain"] == "platform"
    # The verification is itself audited (platform chain).
    rows = _chain_rows(sdb.url("platform"), "platform")
    assert any(r["action"] == "platform.audit.chain_verified" and r["outcome"] == "failure" for r in rows)


# --------------------------------------------------------------------------- tenant API
def test_tenant_api_lists_only_own_events_with_filters_and_pagination(harness, sdb):
    a, b = uuid.uuid4(), uuid.uuid4()
    consume(
        sdb.app_url,
        *[_event(a, "lead.exported") for _ in range(3)],
        _event(a, "campaign.launched", outcome="denied"),
        _event(b, "lead.exported"),
    )
    _, tok = harness.member(a, {"audit:read"})
    r = harness.client.get("/api/v1/audit/events", headers=auth(tok, a), params={"limit": 2})
    assert r.status_code == 200
    page1 = r.json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    assert set(page1["items"][0]) == {
        "id",
        "occurred_at",
        "actor_type",
        "actor_id",
        "action",
        "resource_type",
        "resource_id",
        "outcome",
        "request_id",
        "correlation_id",
        "producer",
        "metadata",
    }
    page2 = harness.client.get(
        "/api/v1/audit/events", headers=auth(tok, a), params={"limit": 2, "cursor": page1["next_cursor"]}
    ).json()
    ids = {i["id"] for i in page1["items"]} | {i["id"] for i in page2["items"]}
    assert len(ids) == 4 and page2["next_cursor"] is None
    only_lead = harness.client.get("/api/v1/audit/events", headers=auth(tok, a), params={"action": "lead."}).json()
    assert len(only_lead["items"]) == 3
    denied = harness.client.get("/api/v1/audit/events", headers=auth(tok, a), params={"outcome": "denied"}).json()
    assert [i["action"] for i in denied["items"]] == ["campaign.launched"]
    wildcard = harness.client.get("/api/v1/audit/events", headers=auth(tok, a), params={"action": "%"})
    assert wildcard.status_code == 422  # LIKE wildcards are not accepted as a prefix
    now = datetime.now(UTC)
    window = {"from": (now - timedelta(hours=1)).isoformat(), "to": (now + timedelta(hours=1)).isoformat()}
    assert len(harness.client.get("/api/v1/audit/events", headers=auth(tok, a), params=window).json()["items"]) == 4
    future = {"from": (now + timedelta(hours=1)).isoformat()}
    assert harness.client.get("/api/v1/audit/events", headers=auth(tok, a), params=future).json()["items"] == []
    inverted = {"from": window["to"], "to": window["from"]}
    assert harness.client.get("/api/v1/audit/events", headers=auth(tok, a), params=inverted).status_code == 422


def test_cross_tenant_and_permission_boundaries(harness, sdb):
    a, b = uuid.uuid4(), uuid.uuid4()
    consume(sdb.app_url, _event(b))
    _, reader_a = harness.member(a, {"audit:read"})
    assert harness.client.get("/api/v1/audit/events", headers=auth(reader_a, b)).status_code == 403
    assert harness.client.post("/api/v1/audit/exports", headers=auth(reader_a, b), json={}).status_code == 403
    _, no_perm = harness.member(a, {"analytics:read"})
    assert harness.client.get("/api/v1/audit/events", headers=auth(no_perm, a)).status_code == 403
    assert harness.client.get("/api/v1/audit/events", headers=auth(reader_a)).status_code == 403  # no tenant
    # Tenant users never reach the platform API.
    for path in ("/api/v1/platform/audit/events",):
        assert harness.client.get(path, headers=auth(reader_a, a)).status_code == 403
    assert harness.client.post("/api/v1/platform/audit/verify", headers=auth(reader_a), json={}).status_code == 403
    assert harness.client.get("/api/v1/audit/events").status_code == 401


def test_export_requires_step_up_writes_tenant_csv_and_is_audited(harness, sdb):
    tenant = uuid.uuid4()
    consume(
        sdb.app_url,
        _event(tenant, "lead.exported", metadata={"note": "=HYPERLINK(1)"}),
        *[_event(tenant, f"x.e{i}") for i in range(6)],
    )
    uid, tok = harness.member(tenant, {"audit:read"})
    r = harness.client.post("/api/v1/audit/exports", headers=auth(tok, tenant), json={})
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    _, strong = harness.member(tenant, {"audit:read"}, mfa=True)
    r = harness.client.post("/api/v1/audit/exports", headers=auth(strong, tenant), json={"action": "lead."})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rows"] == 1 and body["truncated"] is False and body["expires_in_seconds"] == 300
    [(bucket, key)] = list(harness.store.objects)
    assert bucket == "exports" and key.startswith(f"{tenant}/audit/")
    csv_text = harness.store.objects[(bucket, key)][0].decode()
    assert "lead.exported" in csv_text and "x.e1" not in csv_text
    many = harness.client.post("/api/v1/audit/exports", headers=auth(strong, tenant), json={}).json()
    assert many["rows"] == 5 and many["truncated"] is True  # bounded (max rows = 5 in tests)
    unknown = harness.client.post("/api/v1/audit/exports", headers=auth(strong, tenant), json={"limit": 10})
    assert unknown.status_code == 422
    listed = harness.client.get(
        "/api/v1/audit/events", headers=auth(strong, tenant), params={"action": "audit.export"}
    ).json()["items"]
    assert len(listed) == 2 and listed[0]["metadata"]["rows"] in (1, 5)
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    empty = harness.client.post("/api/v1/audit/exports", headers=auth(strong, tenant), json={"from": later})
    assert empty.status_code == 201 and empty.json()["rows"] == 0


def test_csv_cells_cannot_inject_formulas():
    from audit_service.api.events import _cell

    assert _cell("=SUM(A1)") == "'=SUM(A1)" and _cell("+1") == "'+1" and _cell("@x") == "'@x"
    assert _cell("lead.exported") == "lead.exported" and _cell(None) == ""


# --------------------------------------------------------------------------- platform API
def test_platform_search_spans_tenants_and_is_itself_audited(harness, sdb):
    a, b = uuid.uuid4(), uuid.uuid4()
    consume(sdb.app_url, _event(a, "lead.exported"), _event(b, "lead.exported"), _event(None, "platform.x"))
    uid, op = harness.platform_user({"platform:audit:read"})
    every = harness.client.get("/api/v1/platform/audit/events", headers=auth(op), params={"limit": 200})
    assert every.status_code == 200, every.text
    tenants = {i["tenant_id"] for i in every.json()["items"]}
    assert {str(a), str(b), None} <= tenants
    scoped = harness.client.get(
        "/api/v1/platform/audit/events", headers=auth(op), params={"tenant_id": str(a), "action": "lead."}
    ).json()["items"]
    assert [i["tenant_id"] for i in scoped] == [str(a)]
    only_platform = harness.client.get(
        "/api/v1/platform/audit/events", headers=auth(op), params={"platform_only": "true", "action": "platform.x"}
    ).json()["items"]
    assert only_platform and all(i["tenant_id"] is None for i in only_platform)

    # The tenant-scoped search is visible in tenant A's own audit log, with who, filters and result count.
    _, reader = harness.member(a, {"audit:read"})
    log = harness.client.get(
        "/api/v1/audit/events", headers=auth(reader, a), params={"action": "platform.audit.searched"}
    ).json()["items"]
    assert len(log) == 1 and log[0]["actor_id"] == uid
    assert log[0]["metadata"]["result_count"] == 1 and log[0]["metadata"]["action_prefix"] == "lead."
    # Unscoped searches land in the platform chain.
    rows = _chain_rows(sdb.url("platform"), "platform")
    assert sum(r["action"] == "platform.audit.searched" and r["actor_id"] == uid for r in rows) == 2

    _, no_perm = harness.platform_user({"platform:tenants:read"})
    assert harness.client.get("/api/v1/platform/audit/events", headers=auth(no_perm)).status_code == 403


def test_runtime_role_without_tenant_context_sees_only_the_platform_chain(sdb):
    consume(sdb.app_url, _event(uuid.uuid4()), _event(None, "platform.y"))

    async def tenants_visible(db):
        async with db.global_session() as s:
            return set((await s.execute(select(audit_events.c.tenant_id).distinct())).scalars())

    assert run_db(sdb.app_url, tenants_visible) == {None}
    assert len(run_db(sdb.url("platform"), tenants_visible)) > 1
