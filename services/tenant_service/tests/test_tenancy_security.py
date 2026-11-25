"""Tenant/IAM behaviour + security boundaries against a real database (build.md §15, §22)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from nexus_core.db import Database
from nexus_core.testing import auth

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _run(coro):
    return asyncio.run(coro)


async def _grant_platform(app_url: str, user_id: str, email: str, role: str) -> None:
    db = Database(app_url, pool_size=1)
    async with db.global_session() as s:
        await s.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT (id) DO NOTHING"),
            {"i": user_id, "e": email},
        )
        await s.execute(
            text("INSERT INTO platform_role_assignments (user_id, role_key) VALUES (:i, :r) " "ON CONFLICT DO NOTHING"),
            {"i": user_id, "r": role},
        )
    await db.dispose()


@pytest.fixture
def platform_owner(harness, sdb):
    email = f"po-{uuid.uuid4().hex[:6]}@nexus.local"
    uid, _ = harness.user(email, roles=["platform_owner"])
    _run(_grant_platform(sdb.app_url, uid, email, "platform_owner"))
    return uid, email


def _po_token(harness, platform_owner, mfa=True):
    uid, email = platform_owner
    return harness.tokens.user(uid, email, roles=["platform_owner"], mfa=mfa)


def _create_tenant(harness, platform_owner, slug=None):
    slug = slug or f"t-{uuid.uuid4().hex[:8]}"
    owner_email = f"owner@{slug}.test"
    resp = harness.client.post(
        "/api/v1/platform/tenants",
        headers=auth(_po_token(harness, platform_owner)),
        json={"name": f"Tenant {slug}", "slug": slug, "owner_email": owner_email},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], owner_email, harness.last_invite_token()


def _accept(harness, email, token, *, mfa=True):
    uid, jwt_ = harness.user(email, mfa=mfa)
    jwt_ = harness.tokens.user(uid, email, mfa=mfa)
    resp = harness.client.post("/api/v1/invitations/accept", headers=auth(jwt_), json={"token": token})
    return uid, jwt_, resp


# --------------------------------------------------------------------------- platform gatekeeping
def test_platform_role_in_token_alone_is_not_enough(harness):
    """A forged/misconfigured realm role without a DB assignment yields no platform permissions."""
    uid, _ = harness.user("rogue@nexus.local", roles=["platform_owner"])
    tok = harness.tokens.user(uid, "rogue@nexus.local", roles=["platform_owner"], mfa=True)
    assert harness.client.get("/api/v1/platform/tenants", headers=auth(tok)).status_code == 403


def test_high_risk_platform_action_requires_step_up(harness, platform_owner):
    resp = harness.client.post(
        "/api/v1/platform/tenants",
        headers=auth(_po_token(harness, platform_owner, mfa=False)),
        json={"name": "No MFA", "slug": "no-mfa-co", "owner_email": "x@no-mfa.test"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "step_up_required"


def test_tenant_creation_and_owner_invitation_flow(harness, platform_owner):
    tenant_id, owner_email, token = _create_tenant(harness, platform_owner)
    preview = harness.client.post("/api/v1/invitations/preview", json={"token": token})
    assert preview.status_code == 200 and preview.json()["role_keys"] == ["tenant_owner"]
    assert "@" in preview.json()["email_masked"] and owner_email not in preview.text

    # Owner role requires MFA at acceptance.
    _, _, resp = _accept(harness, owner_email, token, mfa=False)
    assert resp.status_code == 401 and resp.json()["code"] == "step_up_required"
    _, owner_jwt, resp = _accept(harness, owner_email, token, mfa=True)
    assert resp.status_code == 200, resp.text

    # Single use: the same token can never be accepted again.
    _, _, again = _accept(harness, owner_email, token)
    assert again.status_code == 404
    me = harness.client.get("/api/v1/me", headers=auth(owner_jwt)).json()
    assert [t["id"] for t in me["tenants"]] == [tenant_id]


def test_invitation_requires_matching_verified_email(harness, platform_owner):
    _, _, token = _create_tenant(harness, platform_owner)
    _, _, resp = _accept(harness, "attacker@evil.test", token)
    assert resp.status_code == 403
    uid, _ = harness.user("unverified@x.test")
    tok = harness.tokens.user(uid, "unverified@x.test", verified=False, mfa=True)
    assert harness.client.post("/api/v1/invitations/accept", headers=auth(tok), json={"token": token}).status_code in (
        403,
        404,
    )


def test_invalid_tokens_are_indistinguishable(harness):
    a = harness.client.post("/api/v1/invitations/preview", json={"token": "x" * 43})
    b = harness.client.post("/api/v1/invitations/preview", json={"token": "y" * 43})
    assert a.status_code == b.status_code == 404 and a.json()["detail"] == b.json()["detail"]
    assert harness.client.post("/api/v1/invitations/onboard", json={"token": "z" * 43}).status_code == 202


def _owner_of_new_tenant(harness, platform_owner):
    tenant_id, owner_email, token = _create_tenant(harness, platform_owner)
    _, owner_jwt, resp = _accept(harness, owner_email, token)
    assert resp.status_code == 200
    return tenant_id, owner_jwt


def _invite_and_accept(harness, tenant_id, inviter_jwt, email, roles, mfa=True):
    r = harness.client.post(
        "/api/v1/invitations", headers=auth(inviter_jwt, tenant_id), json={"email": email, "role_keys": roles}
    )
    assert r.status_code == 201, r.text
    uid, jwt_, resp = _accept(harness, email, harness.last_invite_token(), mfa=mfa)
    assert resp.status_code == 200, resp.text
    return uid, jwt_


# --------------------------------------------------------------------------- tenant isolation
def test_cross_tenant_access_is_denied(harness, platform_owner):
    tenant_a, owner_a = _owner_of_new_tenant(harness, platform_owner)
    tenant_b, owner_b = _owner_of_new_tenant(harness, platform_owner)
    assert harness.client.get("/api/v1/memberships", headers=auth(owner_a, tenant_a)).status_code == 200
    for path in ("/api/v1/memberships", "/api/v1/invitations", "/api/v1/settings", "/api/v1/teams"):
        r = harness.client.get(path, headers=auth(owner_a, tenant_b))
        assert r.status_code == 403, path
    # Unknown tenant id → same response as a foreign tenant (no enumeration).
    r = harness.client.get("/api/v1/memberships", headers=auth(owner_a, uuid.uuid4()))
    assert r.status_code == 403
    # Tenant admins cannot reach platform APIs.
    for path in ("/api/v1/platform/tenants", "/api/v1/platform/users", "/api/v1/platform/settings"):
        assert harness.client.get(path, headers=auth(owner_a)).status_code == 403


def test_rls_blocks_cross_tenant_rows_even_with_raw_sql(harness, platform_owner, sdb):
    tenant_a, _ = _owner_of_new_tenant(harness, platform_owner)
    tenant_b, _ = _owner_of_new_tenant(harness, platform_owner)

    async def probe():
        db = Database(sdb.app_url, pool_size=1)
        async with db.tenant_session(tenant_a) as s:
            seen = set((await s.execute(text("SELECT DISTINCT tenant_id FROM memberships"))).scalars())
            invites = set((await s.execute(text("SELECT DISTINCT tenant_id FROM invitations"))).scalars())
        async with db.global_session() as s:
            none = (await s.execute(text("SELECT count(*) FROM memberships"))).scalar_one()
        await db.dispose()
        return seen, invites, none

    seen, invites, none = _run(probe())
    assert {str(t) for t in seen} == {tenant_a} and {str(t) for t in invites} == {tenant_a}
    assert none == 0


# --------------------------------------------------------------------------- privilege escalation
def test_users_cannot_grant_more_than_they_hold(harness, platform_owner):
    tenant_id, owner_jwt = _owner_of_new_tenant(harness, platform_owner)
    _, admin_jwt = _invite_and_accept(
        harness, tenant_id, owner_jwt, f"admin-{uuid.uuid4().hex[:5]}@x.test", ["tenant_admin"]
    )
    _, cm_jwt = _invite_and_accept(
        harness, tenant_id, owner_jwt, f"cm-{uuid.uuid4().hex[:5]}@x.test", ["campaign_manager"], mfa=False
    )
    # Campaign manager lacks tenant:users:manage entirely.
    r = harness.client.post(
        "/api/v1/invitations", headers=auth(cm_jwt, tenant_id), json={"email": "x@y.test", "role_keys": ["analyst"]}
    )
    assert r.status_code == 403
    # Tenant admin cannot mint tenant owners (escalation).
    r = harness.client.post(
        "/api/v1/invitations",
        headers=auth(admin_jwt, tenant_id),
        json={"email": "boss@y.test", "role_keys": ["tenant_owner"]},
    )
    assert r.status_code == 403
    # Roles endpoint reflects grantability.
    roles = {
        r["key"]: r["grantable"]
        for r in harness.client.get("/api/v1/roles", headers=auth(admin_jwt, tenant_id)).json()["items"]
    }
    assert roles["tenant_owner"] is False and roles["analyst"] is True


def test_last_owner_cannot_be_removed_and_no_self_edit(harness, platform_owner):
    tenant_id, owner_jwt = _owner_of_new_tenant(harness, platform_owner)
    members = harness.client.get("/api/v1/memberships", headers=auth(owner_jwt, tenant_id)).json()["items"]
    me = members[0]
    r = harness.client.patch(
        f"/api/v1/memberships/{me['id']}",
        headers=auth(owner_jwt, tenant_id),
        json={"status": "deactivated", "expected_version": me["version"]},
    )
    assert r.status_code == 403  # self-edit blocked

    _, owner2_jwt = _invite_and_accept(
        harness, tenant_id, owner_jwt, f"o2-{uuid.uuid4().hex[:5]}@x.test", ["tenant_owner"]
    )
    members = harness.client.get("/api/v1/memberships", headers=auth(owner2_jwt, tenant_id)).json()["items"]
    first = next(m for m in members if m["id"] == me["id"])
    r = harness.client.patch(
        f"/api/v1/memberships/{first['id']}",
        headers=auth(owner2_jwt, tenant_id),
        json={"role_keys": ["analyst"], "expected_version": first["version"]},
    )
    assert r.status_code == 200  # two owners → demoting one is allowed
    r = harness.client.get("/api/v1/memberships", headers=auth(owner_jwt, tenant_id))
    assert r.status_code == 403  # demoted owner lost tenant:users:manage immediately


def test_resend_supersedes_previous_token_and_revoke_works(harness, platform_owner):
    tenant_id, owner_jwt = _owner_of_new_tenant(harness, platform_owner)
    email = f"late-{uuid.uuid4().hex[:5]}@x.test"
    inv = harness.client.post(
        "/api/v1/invitations", headers=auth(owner_jwt, tenant_id), json={"email": email, "role_keys": ["analyst"]}
    ).json()
    first_token = harness.last_invite_token()
    re = harness.client.post(f"/api/v1/invitations/{inv['id']}/resend", headers=auth(owner_jwt, tenant_id))
    assert re.status_code == 200
    second_token = harness.last_invite_token()
    assert first_token != second_token
    assert harness.client.post("/api/v1/invitations/preview", json={"token": first_token}).status_code == 404
    revoke = harness.client.post(f"/api/v1/invitations/{re.json()['id']}/revoke", headers=auth(owner_jwt, tenant_id))
    assert revoke.status_code == 200
    assert harness.client.post("/api/v1/invitations/preview", json={"token": second_token}).status_code == 404


def test_bulk_invitations_report_per_row(harness, platform_owner):
    tenant_id, owner_jwt = _owner_of_new_tenant(harness, platform_owner)
    r = harness.client.post(
        "/api/v1/invitations/bulk",
        headers=auth(owner_jwt, tenant_id),
        json={
            "rows": [
                {"email": "b1@x.test", "role_keys": ["analyst"]},
                {"email": "b2@x.test", "role_keys": ["no_such_role"]},
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {"total": 2, "invited": 1, "failed": 1}


# --------------------------------------------------------------------------- settings guardrails
def test_tenant_settings_bounded_by_platform_and_locked_controls(harness, platform_owner):
    tenant_id, owner_jwt = _owner_of_new_tenant(harness, platform_owner)
    comm = harness.client.get("/api/v1/settings/communication", headers=auth(owner_jwt, tenant_id)).json()
    version = comm.pop("_version")
    comm["max_concurrent_calls"] = 10_000
    r = harness.client.put(
        "/api/v1/settings/communication",
        headers=auth(owner_jwt, tenant_id),
        json={"data": comm, "expected_version": version},
    )
    assert r.status_code == 422 and "platform limit" in r.json()["detail"]
    ai = harness.client.get("/api/v1/settings/ai", headers=auth(owner_jwt, tenant_id)).json()
    v = ai.pop("_version")
    ai["draft_auto_send"] = True  # tenants cannot switch on AI auto-send
    assert (
        harness.client.put(
            "/api/v1/settings/ai", headers=auth(owner_jwt, tenant_id), json={"data": ai, "expected_version": v}
        ).status_code
        == 422
    )
    comm["max_concurrent_calls"] = 3
    ok = harness.client.put(
        "/api/v1/settings/communication",
        headers=auth(owner_jwt, tenant_id),
        json={"data": comm, "expected_version": version},
    )
    assert ok.status_code == 200 and ok.json()["_version"] == version + 1
    stale = harness.client.put(
        "/api/v1/settings/communication",
        headers=auth(owner_jwt, tenant_id),
        json={"data": comm, "expected_version": version},
    )
    assert stale.status_code == 409


# --------------------------------------------------------------------------- support / dual control
def test_support_access_requires_a_second_admin(harness, platform_owner, sdb):
    tenant_id, owner_jwt = _owner_of_new_tenant(harness, platform_owner)
    po = _po_token(harness, platform_owner)
    r = harness.client.post(
        "/api/v1/platform/support-grants",
        headers=auth(po),
        json={
            "tenant_id": tenant_id,
            "scope": "read_operations",
            "reason": "Customer ticket investigation",
            "ticket_ref": "SUP-123",
            "duration_minutes": 30,
        },
    )
    assert r.status_code == 201
    grant_id = r.json()["id"]
    # Requester cannot approve their own request.
    self_ok = harness.client.post(
        f"/api/v1/platform/support-grants/{grant_id}/decision",
        params={"tenant_id": tenant_id},
        headers=auth(po),
        json={"approve": True},
    )
    assert self_ok.status_code == 403
    # Before approval the platform admin has no access to the tenant.
    assert harness.client.get("/api/v1/me/access", headers=auth(po, tenant_id)).status_code == 403
    email2 = f"po2-{uuid.uuid4().hex[:6]}@nexus.local"
    uid2, _ = harness.user(email2, roles=["platform_owner"])
    _run(_grant_platform(sdb.app_url, uid2, email2, "platform_owner"))
    po2 = harness.tokens.user(uid2, email2, roles=["platform_owner"], mfa=True)
    ok = harness.client.post(
        f"/api/v1/platform/support-grants/{grant_id}/decision",
        params={"tenant_id": tenant_id},
        headers=auth(po2),
        json={"approve": True},
    )
    assert ok.status_code == 200
    access = harness.client.get("/api/v1/me/access", headers=auth(po, tenant_id))
    assert access.status_code == 200
    perms = set(access.json()["permissions"])
    assert "calls:read" in perms and not any(p.endswith((":manage", ":export", ":launch")) for p in perms)
    # Tenant can see the grant.
    grants = harness.client.get("/api/v1/support-grants", headers=auth(owner_jwt, tenant_id)).json()["items"]
    assert grants[0]["id"] == grant_id and grants[0]["status"] == "active"


def test_internal_endpoints_reject_users_and_unknown_services(harness, platform_owner):
    tenant_id, owner_jwt = _owner_of_new_tenant(harness, platform_owner)
    body = {"sub": str(uuid.uuid4()), "tenant_id": tenant_id}
    assert harness.client.post("/internal/v1/authz/resolve", headers=auth(owner_jwt), json=body).status_code == 403
    rogue = harness.tokens.service("svc-unknown")
    assert harness.client.post("/internal/v1/authz/resolve", headers=auth(rogue), json=body).status_code == 403
    good = harness.tokens.service("svc-crm-service")
    r = harness.client.post("/internal/v1/authz/resolve", headers=auth(good), json=body)
    assert r.status_code == 200 and r.json()["is_member"] is False
