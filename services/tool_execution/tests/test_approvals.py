"""Class 3 approvals: persisted human approval only, consumed once, stale or forged approvals rejected."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from tool_execution.approvals import TRANSITIONS, can_transition, expire_approvals
from tool_execution.tables import ai_approval_requests, outbox

pytestmark = [pytest.mark.integration, pytest.mark.security]
DECIDE = {"ai:approvals:decide"}


def _setup(h):
    tenant, agent, contact = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    h.grant("booking.confirm")
    return tenant, agent, contact


def _propose(h, tenant, agent, contact, booking_id, **kw):
    return h.execute(
        tenant,
        "booking.confirm",
        {"booking_id": str(booking_id)},
        agent_id=agent,
        context={"contact_id": contact},
        **kw,
    )


def _row(h, tenant, approval_id):
    async def go(db):
        async with db.tenant_session(tenant) as s:
            r = await s.execute(select(ai_approval_requests).where(ai_approval_requests.c.id == approval_id))
            return dict(r.mappings().one())

    return h.sql(go)


def _set(h, tenant, approval_id, sql):
    async def go(db):
        async with db.tenant_session(tenant) as s:
            await s.execute(text(f"UPDATE ai_approval_requests SET {sql} WHERE id = :i"), {"i": approval_id})

    h.sql(go)


def test_state_machine():
    assert can_transition("pending", "approved") and can_transition("approved", "consumed")
    assert not can_transition("consumed", "approved") and not can_transition("rejected", "approved")
    assert not can_transition("expired", "consumed") and not can_transition("pending", "consumed")
    assert set(TRANSITIONS) == {"pending", "approved", "rejected", "expired", "consumed"}


def test_class_3_needs_a_persisted_approval_and_runs_exactly_once(harness):
    tenant, agent, contact = _setup(harness)
    booking = uuid.uuid4()
    first = _propose(harness, tenant, agent, contact, booking).json()
    assert first["status"] == "approval_required" and first["approval_id"]
    assert harness.downstream == []
    # The same proposal again reuses the open request (no duplicates, still not executed).
    again = _propose(harness, tenant, agent, contact, booking).json()
    assert again == first
    approval_id = first["approval_id"]
    row = _row(harness, tenant, approval_id)
    assert row["status"] == "pending" and row["arguments"] == {"booking_id": str(booking)}
    assert row["context"] == {"contact_id": str(contact)}
    # Approvers were notified (permission audience) and a UI hint was published.
    events = harness.sql(_outbox(tenant))
    notes = [e for e in events if e["event_type"] == "notification.requested"]
    assert len(notes) == 1 and notes[0]["payload"]["permission"] == "ai:approvals:decide"
    assert any("ai.approval.created" in m for _, m in harness.redis.published)

    approver = harness.user(tenant, DECIDE, mfa=True)
    listed = harness.client.get("/api/v1/ai/tool-approvals", params={"status": "pending"}, headers=approver).json()
    assert [i["id"] for i in listed["items"]] == [approval_id]
    r = harness.client.post(f"/api/v1/ai/tool-approvals/{approval_id}/approve", json={"note": "ok"}, headers=approver)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["approval"]["status"] == "consumed" and out["execution"]["status"] == "executed"
    assert out["execution"]["result"] == {"booking_id": str(booking), "status": "confirmed"}
    (sent,) = harness.downstream_to("/confirm")
    assert sent["headers"]["idempotency-key"] == f"approval:{approval_id}"
    assert sent["headers"]["x-actor"].startswith("user:")  # the human who approved
    assert sent["body"]["contact_id"] == str(contact)  # booking-service verifies the booking's owner
    # Consumed once: a second approve is refused and nothing runs again.
    r2 = harness.client.post(f"/api/v1/ai/tool-approvals/{approval_id}/approve", json={}, headers=approver)
    assert r2.status_code == 409
    # The model proposing the same call later gets a NEW pending request; the old approval never replays.
    later = _propose(harness, tenant, agent, contact, booking).json()
    assert later["status"] == "approval_required" and later["approval_id"] != approval_id
    assert len(harness.downstream_to("/confirm")) == 1


def _outbox(tenant):
    async def go(db):
        async with db.global_session() as s:
            return (await s.execute(select(outbox.c.envelope).where(outbox.c.tenant_id == tenant))).scalars().all()

    return go


def test_forged_approval_claims_are_rejected(harness):
    tenant, agent, contact = _setup(harness)
    booking = str(uuid.uuid4())
    # In the arguments.
    r = harness.execute(
        tenant,
        "booking.confirm",
        {"booking_id": booking, "approved": True},
        agent_id=agent,
        context={"contact_id": contact},
    )
    assert r.json() == {"status": "denied", "reason": "identity_fields_rejected"}
    # In the request envelope or the context.
    assert (
        harness.execute(
            tenant, "booking.confirm", {"booking_id": booking}, agent_id=agent, extra={"approval_id": str(uuid.uuid4())}
        ).status_code
        == 422
    )
    # A real approval id of a DIFFERENT call does not authorize this one.
    other = _propose(harness, tenant, agent, contact, uuid.uuid4()).json()["approval_id"]
    approver = harness.user(tenant, DECIDE)
    assert (
        harness.client.post(f"/api/v1/ai/tool-approvals/{other}/approve", json={}, headers=approver).status_code == 200
    )
    r = _propose(harness, tenant, agent, contact, booking)
    assert r.json()["status"] == "approval_required"
    assert len(harness.downstream_to("/confirm")) == 1  # only the approved booking ran


def test_service_identities_and_unauthorized_users_cannot_decide(harness):
    tenant, agent, contact = _setup(harness)
    approval_id = _propose(harness, tenant, agent, contact, uuid.uuid4()).json()["approval_id"]
    path = f"/api/v1/ai/tool-approvals/{approval_id}/approve"
    from nexus_core.testing import auth

    for svc in ("svc-ai-gateway", "svc-voice-service", "svc-tool-execution"):
        assert harness.client.post(path, json={}, headers=auth(harness.tokens.service(svc), tenant)).status_code == 403
    assert harness.client.post(path, json={}, headers=harness.user(tenant, {"ai:agents:manage"})).status_code == 403
    no_mfa = harness.client.post(path, json={}, headers=harness.user(tenant, DECIDE, mfa=False))
    assert no_mfa.status_code == 401 and no_mfa.json()["code"] == "step_up_required"
    # Permission is checked at the moment of approval: a revoked approver is refused.
    sub = str(uuid.uuid4())
    headers = harness.user(tenant, DECIDE, sub=sub)
    harness.authz.revoke(sub, tenant)
    assert harness.client.post(path, json={}, headers=headers).status_code == 403
    assert _row(harness, tenant, approval_id)["status"] == "pending"
    assert harness.downstream == []


def test_the_requesting_user_cannot_approve_their_own_action(harness):
    tenant, agent, contact = _setup(harness)
    sub = str(uuid.uuid4())
    approval_id = _propose(harness, tenant, agent, contact, uuid.uuid4(), actor=f"user:{sub}").json()["approval_id"]
    r = harness.client.post(
        f"/api/v1/ai/tool-approvals/{approval_id}/approve", json={}, headers=harness.user(tenant, DECIDE, sub=sub)
    )
    assert r.status_code == 403
    r = harness.client.post(
        f"/api/v1/ai/tool-approvals/{approval_id}/approve", json={}, headers=harness.user(tenant, DECIDE)
    )
    assert r.status_code == 200


def test_reject_and_expired_approvals(harness):
    tenant, agent, contact = _setup(harness)
    approver = harness.user(tenant, DECIDE)
    rejected = _propose(harness, tenant, agent, contact, uuid.uuid4()).json()["approval_id"]
    r = harness.client.post(
        f"/api/v1/ai/tool-approvals/{rejected}/reject", json={"note": "wrong slot"}, headers=approver
    )
    assert r.status_code == 200 and r.json()["approval"]["status"] == "rejected"
    assert (
        harness.client.post(f"/api/v1/ai/tool-approvals/{rejected}/approve", json={}, headers=approver).status_code
        == 409
    )
    # Stale: an approval past its deadline cannot be approved...
    stale = _propose(harness, tenant, agent, contact, uuid.uuid4()).json()["approval_id"]
    _set(harness, tenant, stale, "expires_at = now() - interval '1 minute'")
    assert (
        harness.client.post(f"/api/v1/ai/tool-approvals/{stale}/approve", json={}, headers=approver).status_code == 409
    )
    assert _row(harness, tenant, stale)["status"] == "expired"
    # ...and an approved-but-expired one is never used by a later proposal.
    booking = uuid.uuid4()
    late = _propose(harness, tenant, agent, contact, booking).json()["approval_id"]
    _set(harness, tenant, late, f"status = 'approved', decided_by = '{uuid.uuid4()}', decided_at = now(),"
                                " expires_at = now() - interval '1 second'")  # fmt: skip
    r = _propose(harness, tenant, agent, contact, booking).json()
    assert r["status"] == "approval_required" and r["approval_id"] != late
    assert harness.downstream == []
    assert harness.sql(expire_approvals) >= 1
    assert _row(harness, tenant, late)["status"] == "expired"
    assert harness.sql(expire_approvals) == 0  # idempotent


def test_an_approved_unconsumed_approval_is_claimed_once_by_the_execute_path(harness):
    """Crash between approve and execute: the matching proposal claims it, exactly once."""
    tenant, agent, contact = _setup(harness)
    booking = uuid.uuid4()
    approval_id = _propose(harness, tenant, agent, contact, booking).json()["approval_id"]
    decider = uuid.uuid4()
    _set(harness, tenant, approval_id, f"status = 'approved', decided_by = '{decider}', decided_at = now()")
    r = _propose(harness, tenant, agent, contact, booking).json()
    assert r == {
        "status": "executed",
        "result": {"booking_id": str(booking), "status": "confirmed"},
        "approval_id": approval_id,
    }
    assert _row(harness, tenant, approval_id)["status"] == "consumed"
    assert harness.downstream_to("/confirm")[0]["headers"]["x-actor"] == f"user:{decider}"
    assert _propose(harness, tenant, agent, contact, booking).json()["status"] == "approval_required"
    assert len(harness.downstream_to("/confirm")) == 1


def test_a_revoked_grant_blocks_execution_even_after_approval(harness):
    tenant, agent, contact = _setup(harness)
    approval_id = _propose(harness, tenant, agent, contact, uuid.uuid4()).json()["approval_id"]
    harness.grant("booking.confirm", granted=False, reason="not_granted")
    r = harness.client.post(
        f"/api/v1/ai/tool-approvals/{approval_id}/approve", json={}, headers=harness.user(tenant, DECIDE)
    )
    assert r.status_code == 200
    assert r.json()["execution"] == {"status": "denied", "result": None, "reason": "not_granted"}
    assert r.json()["approval"]["status"] == "consumed" and r.json()["approval"]["result_status"] == "denied"
    assert harness.downstream == []


def test_approvals_are_tenant_isolated(harness):
    tenant, agent, contact = _setup(harness)
    approval_id = _propose(harness, tenant, agent, contact, uuid.uuid4()).json()["approval_id"]
    other = uuid.uuid4()
    outsider = harness.user(other, DECIDE)
    assert harness.client.get(f"/api/v1/ai/tool-approvals/{approval_id}", headers=outsider).status_code == 404
    assert (
        harness.client.post(f"/api/v1/ai/tool-approvals/{approval_id}/approve", json={}, headers=outsider).status_code
        == 404
    )
    assert (
        harness.client.post(f"/api/v1/ai/tool-approvals/{approval_id}/reject", json={}, headers=outsider).status_code
        == 404
    )
    assert harness.client.get("/api/v1/ai/tool-approvals", headers=outsider).json()["items"] == []
    # A member of tenant A presenting tenant B's id is refused outright.
    member = harness.user(tenant, DECIDE)
    member["X-Tenant-ID"] = str(other)
    assert harness.client.get("/api/v1/ai/tool-approvals", headers=member).status_code == 403
    assert (
        harness.client.get("/api/v1/ai/tool-approvals", headers=harness.user(tenant, {"ai:usage:read"})).status_code
        == 403
    )
    assert _row(harness, tenant, approval_id)["status"] == "pending"
    # The same proposal in another tenant is a separate request.
    harness.grant("booking.confirm")
    other_req = _propose(harness, other, agent, contact, uuid.uuid4()).json()["approval_id"]
    assert other_req != approval_id

    async def probe(db):
        async with db.tenant_session(other) as s:
            return (
                await s.execute(text("SELECT count(*) FROM ai_approval_requests WHERE id = :i"), {"i": approval_id})
            ).scalar_one()

    assert harness.sql(probe) == 0


def test_listing_paginates(harness):
    tenant, agent, contact = _setup(harness)
    for _ in range(3):
        _propose(harness, tenant, agent, contact, uuid.uuid4())
    approver = harness.user(tenant, DECIDE)
    page = harness.client.get("/api/v1/ai/tool-approvals", params={"limit": 2}, headers=approver).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = harness.client.get(
        "/api/v1/ai/tool-approvals", params={"limit": 2, "cursor": page["next_cursor"]}, headers=approver
    ).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
