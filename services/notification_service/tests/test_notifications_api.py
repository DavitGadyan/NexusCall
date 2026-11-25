"""Public and internal notification APIs against a real database (RLS as notify_app)."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select

from nexus_core.testing import auth
from nexus_providers.email import PermanentSendError, TransientSendError
from notification_service.tables import email_log

pytestmark = [pytest.mark.integration, pytest.mark.security]
PUBLIC = "https://app.nexus.test"
INVITE = {
    "template": "invitation",
    "to": "Ada@Acme.test",
    "category": "transactional",
    "variables": {
        "tenant_name": "Acme <b>",
        "inviter": "Owner",
        "roles": "Analyst",
        "link": f"{PUBLIC}/invite/accept?token=secret-token",
        "expires_at": "2026-12-01",
    },
}


def _in_app(h, tenant, *, key=None, **body):
    headers = auth(h.service("svc-booking-service"), tenant)
    if key:
        headers["Idempotency-Key"] = key
    payload = {"title": "Booking soon", "body": "A booking starts in 1 hour.", "kind": "booking_reminder", **body}
    return h.client.post("/internal/v1/notifications/in-app", headers=headers, json=payload)


def _log_rows(run_db, url, rhash):
    async def go(db):
        async with db.global_session() as s:
            return (await s.execute(select(email_log).where(email_log.c.recipient_hash == rhash))).mappings().all()

    return run_db(go, url)


# --------------------------------------------------------------------------- internal: caller allowlist
def test_internal_endpoints_accept_only_allow_listed_services(harness):
    tenant = uuid.uuid4()
    _, user_tok = harness.member(tenant, {"tenant:manage"})
    for path, body in (
        ("/internal/v1/notifications/email", INVITE),
        ("/internal/v1/notifications/in-app", {"permission": "calls:manage", "title": "t", "body": "b"}),
    ):
        assert harness.client.post(path, json=body).status_code == 401
        assert harness.client.post(path, headers=auth(user_tok, tenant), json=body).status_code == 403
        rogue = harness.tokens.service("svc-rogue")
        assert harness.client.post(path, headers=auth(rogue, tenant), json=body).status_code == 403
    assert harness.provider.sent == []


def test_operational_email_is_rendered_escaped_and_logged_without_the_address(harness, run_db, superuser_url):
    r = harness.client.post("/internal/v1/notifications/email", headers=auth(harness.service()), json=INVITE)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "sent"
    [msg] = harness.provider.sent
    assert msg.to_address == "ada@acme.test" and msg.category == "transactional"
    assert "Acme &lt;b&gt;" in msg.html and "Acme <b>" in msg.text
    assert f'href="{PUBLIC}/invite/accept?token=secret-token"' in msg.html
    rhash = hashlib.sha256(b"ada@acme.test").hexdigest()
    assert _log_rows(run_db, None, rhash) == []  # platform-level rows are invisible to the runtime role
    [row] = _log_rows(run_db, superuser_url, rhash)
    assert row["status"] == "sent" and row["tenant_id"] is None and row["template"] == "invitation"
    assert row["requested_by"] == "svc-tenant-service" and row["provider_message_id"] == "msg-1"
    assert "secret-token" not in str(dict(row)) and "acme.test" not in str(dict(row))


def test_email_requests_are_validated_before_anything_is_sent(harness):
    svc = auth(harness.service())
    bad_link = {**INVITE, "variables": {**INVITE["variables"], "link": "https://evil.test/invite"}}
    cases = [
        bad_link,
        {**INVITE, "to": "not-an-address"},
        {**INVITE, "to": "a@b.test\r\nBcc: x@evil.test"},
        {**INVITE, "template": "marketing_blast"},
        {**INVITE, "category": "campaign"},
        {**INVITE, "variables": {**INVITE["variables"], "extra": "x"}},
        {**INVITE, "html": "<p>raw</p>"},
    ]
    for body in cases:
        assert harness.client.post("/internal/v1/notifications/email", headers=svc, json=body).status_code == 422
    assert harness.provider.sent == []


def test_provider_failures_are_reported_to_the_caller_and_logged(harness, run_db):
    svc = auth(harness.service(), uuid.uuid4())
    harness.provider.fail = PermanentSendError("MessageRejected")
    r = harness.client.post("/internal/v1/notifications/email", headers=svc, json={**INVITE, "to": "rej@x.test"})
    assert r.status_code == 422 and r.json()["code"] == "email_rejected"
    harness.provider.fail = TransientSendError("Throttling")
    r = harness.client.post("/internal/v1/notifications/email", headers=svc, json={**INVITE, "to": "tmp@x.test"})
    assert r.status_code == 503

    async def tenant_rows(db):
        tid = svc["X-Tenant-ID"]
        async with db.tenant_session(tid) as s:
            return (await s.execute(select(email_log.c.status, email_log.c.error_code))).all()

    assert sorted(run_db(tenant_rows)) == [("failed", "Throttling"), ("rejected", "MessageRejected")]


def test_per_recipient_rate_limit(harness, fake_redis):
    harness.app.state.mailer._limit = 2
    svc = auth(harness.service())
    codes = [
        harness.client.post("/internal/v1/notifications/email", headers=svc, json=INVITE).status_code for _ in range(3)
    ]
    assert codes == [202, 202, 429] and len(harness.provider.sent) == 2


# --------------------------------------------------------------------------- internal: in-app
def test_in_app_notifications_are_addressed_and_idempotent(harness, fake_redis):
    tenant = uuid.uuid4()
    alice, alice_tok = harness.member(tenant, {"calls:read"})
    _, bob_tok = harness.member(tenant, {"calls:manage"})
    r = _in_app(harness, tenant, key="reminder-1", user_id=str(alice), link="/bookings/1")
    assert r.status_code == 202 and r.json()["duplicate"] is False
    again = _in_app(harness, tenant, key="reminder-1", user_id=str(alice), link="/bookings/1")
    assert again.json() == {"status": "accepted", "id": None, "duplicate": True}
    assert _in_app(harness, tenant, permission="calls:manage", kind="handoff_requested").status_code == 202

    alice_items = harness.client.get("/api/v1/notifications", headers=auth(alice_tok, tenant)).json()
    bob_items = harness.client.get("/api/v1/notifications", headers=auth(bob_tok, tenant)).json()
    assert [i["kind"] for i in alice_items["items"]] == ["booking_reminder"]
    assert [i["kind"] for i in bob_items["items"]] == ["handoff_requested"]
    assert alice_items["unread_count"] == 1 and set(alice_items["items"][0]) == {
        "id",
        "title",
        "body",
        "link",
        "kind",
        "created_at",
        "read_at",
    }
    # Live hints carry ids and audience only.
    assert len(fake_redis.published) == 2 and "Booking soon" not in str(fake_redis.published)


def test_in_app_input_is_validated(harness):
    tenant = uuid.uuid4()
    svc = auth(harness.service("svc-booking-service"), tenant)
    bad = [
        {"title": "t", "body": "b"},  # no audience
        {"user_id": str(uuid.uuid4()), "permission": "calls:read", "title": "t", "body": "b"},
        {"permission": "calls:*", "title": "t", "body": "b"},
        {"permission": "calls:read", "title": "t", "body": "b", "link": "https://evil.test"},
        {"permission": "calls:read", "title": "t", "body": "b", "link": "javascript:alert(1)"},
        {"permission": "calls:read", "title": "t", "body": "b", "kind": "Bad Kind"},
        {"permission": "calls:read", "title": "t", "body": "b", "html": "<b>x</b>"},
    ]
    for body in bad:
        assert harness.client.post("/internal/v1/notifications/in-app", headers=svc, json=body).status_code == 422
    no_tenant = auth(harness.service("svc-booking-service"))
    body = {"permission": "calls:read", "title": "t", "body": "b"}
    assert harness.client.post("/internal/v1/notifications/in-app", headers=no_tenant, json=body).status_code == 403


# --------------------------------------------------------------------------- public API
def test_list_read_and_read_all(harness):
    tenant = uuid.uuid4()
    uid, tok = harness.member(tenant, {"emails:drafts:review", "calls:read"})
    for i in range(3):
        assert _in_app(harness, tenant, user_id=str(uid), title=f"n{i}").status_code == 202
    _in_app(harness, tenant, permission="emails:drafts:review", kind="draft_ready")
    page = harness.client.get("/api/v1/notifications", headers=auth(tok, tenant), params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_cursor"] and page["unread_count"] == 4
    rest = harness.client.get(
        "/api/v1/notifications", headers=auth(tok, tenant), params={"limit": 2, "cursor": page["next_cursor"]}
    ).json()
    assert len(rest["items"]) == 2 and rest["next_cursor"] is None
    first = page["items"][0]["id"]
    r = harness.client.post(f"/api/v1/notifications/{first}/read", headers=auth(tok, tenant))
    assert r.status_code == 200 and r.json()["read_at"]
    again = harness.client.post(f"/api/v1/notifications/{first}/read", headers=auth(tok, tenant))
    assert again.json()["read_at"] == r.json()["read_at"]
    unread = harness.client.get("/api/v1/notifications", headers=auth(tok, tenant), params={"unread": "true"}).json()
    assert len(unread["items"]) == 3 and first not in {i["id"] for i in unread["items"]}
    assert harness.client.post("/api/v1/notifications/read-all", headers=auth(tok, tenant)).json() == {"marked": 3}
    assert harness.client.get("/api/v1/notifications", headers=auth(tok, tenant)).json()["unread_count"] == 0


def test_permission_audience_follows_current_permissions_and_reads_are_per_user(harness):
    tenant = uuid.uuid4()
    reviewer, rtok = harness.member(tenant, {"emails:drafts:review"})
    other, otok = harness.member(tenant, {"emails:drafts:review"})
    _in_app(harness, tenant, permission="emails:drafts:review", kind="draft_ready")
    nid = harness.client.get("/api/v1/notifications", headers=auth(rtok, tenant)).json()["items"][0]["id"]
    harness.client.post(f"/api/v1/notifications/{nid}/read", headers=auth(rtok, tenant))
    assert harness.client.get("/api/v1/notifications", headers=auth(otok, tenant)).json()["unread_count"] == 1
    # Revoking the permission hides the notification immediately.
    harness.authz.grant(other, tenant, {"calls:read"})
    assert harness.client.get("/api/v1/notifications", headers=auth(otok, tenant)).json()["items"] == []
    assert harness.client.post(f"/api/v1/notifications/{nid}/read", headers=auth(otok, tenant)).status_code == 404
    wildcard_user, wtok = harness.member(tenant, {"emails:*"})
    assert len(harness.client.get("/api/v1/notifications", headers=auth(wtok, tenant)).json()["items"]) == 1


def test_cross_tenant_isolation(harness, run_db):
    a, b = uuid.uuid4(), uuid.uuid4()
    ua, tok_a = harness.member(a, {"calls:manage"})
    ub, tok_b = harness.member(b, {"calls:manage"})
    _in_app(harness, b, permission="calls:manage", kind="handoff_requested")
    _in_app(harness, b, user_id=str(ua))  # addressed to A's user id, but inside tenant B
    b_items = harness.client.get("/api/v1/notifications", headers=auth(tok_b, b)).json()["items"]
    nid = b_items[0]["id"]
    assert harness.client.get("/api/v1/notifications", headers=auth(tok_a, a)).json()["items"] == []
    assert harness.client.get("/api/v1/notifications", headers=auth(tok_a, b)).status_code == 403
    assert harness.client.post(f"/api/v1/notifications/{nid}/read", headers=auth(tok_a, a)).status_code == 404
    assert harness.client.post(f"/api/v1/notifications/{nid}/read", headers=auth(tok_a, b)).status_code == 403
    assert harness.client.post("/api/v1/notifications/read-all", headers=auth(tok_a, b)).status_code == 403
    assert harness.client.get("/api/v1/notifications/preferences", headers=auth(tok_a, b)).status_code == 403
    assert harness.client.get("/api/v1/notifications/stream", headers=auth(tok_a, b)).status_code == 403
    assert harness.client.get("/api/v1/notifications", headers=auth(tok_a)).status_code == 403  # no tenant

    async def rls_probe(db):
        from sqlalchemy import text

        async with db.tenant_session(a) as s:
            seen = set((await s.execute(text("SELECT DISTINCT tenant_id FROM notifications"))).scalars())
        async with db.global_session() as s:
            none = (await s.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
        return seen, none

    seen, none = run_db(rls_probe)
    assert seen <= {a} and none == 0


def test_service_principals_cannot_use_the_user_api(harness):
    tenant = uuid.uuid4()
    svc = harness.service("svc-crm-service")
    assert harness.client.get("/api/v1/notifications", headers=auth(svc, tenant)).status_code == 403


def test_preferences_with_versioning_and_muted_kinds(harness):
    tenant = uuid.uuid4()
    uid, tok = harness.member(tenant, {"calls:manage"})
    h = auth(tok, tenant)
    assert harness.client.get("/api/v1/notifications/preferences", headers=h).json() == {
        "email_enabled": True,
        "muted_kinds": [],
        "version": 0,
    }
    _in_app(harness, tenant, permission="calls:manage", kind="handoff_requested")
    _in_app(harness, tenant, user_id=str(uid), kind="booking_reminder")
    r = harness.client.put(
        "/api/v1/notifications/preferences",
        headers=h,
        json={"email_enabled": False, "muted_kinds": ["handoff_requested"], "expected_version": 0},
    )
    assert r.status_code == 200 and r.json()["version"] == 1
    stale = harness.client.put(
        "/api/v1/notifications/preferences", headers=h, json={"muted_kinds": [], "expected_version": 0}
    )
    assert stale.status_code == 409
    items = harness.client.get("/api/v1/notifications", headers=h).json()
    assert [i["kind"] for i in items["items"]] == ["booking_reminder"] and items["unread_count"] == 1
    bad = harness.client.put("/api/v1/notifications/preferences", headers=h, json={"muted_kinds": ["BAD KIND"]})
    assert bad.status_code == 422
    unknown = harness.client.put("/api/v1/notifications/preferences", headers=h, json={"sms": True})
    assert unknown.status_code == 422
