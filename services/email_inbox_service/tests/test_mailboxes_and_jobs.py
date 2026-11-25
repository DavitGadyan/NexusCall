"""Mailbox management, the sweeper, retention and the demo seed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from inbox_support import APPROVE, EICAR, READ, b64

from email_inbox_service.jobs import retention, sweep
from email_inbox_service.seed import seed

pytestmark = [pytest.mark.integration]


@pytest.mark.security
def test_mailbox_management_permissions_uniqueness_and_agent_validation(harness):
    _, manager = harness.user(harness.tenant_a, {"emails:campaigns:manage"})
    _, reader = harness.user(harness.tenant_a, READ)
    body = {"address": "help@acme.example.test", "display_name": "Help"}
    assert harness.client.post("/api/v1/email/mailboxes", headers=reader, json=body).status_code == 403
    assert harness.client.get("/api/v1/email/mailboxes", headers=reader).status_code == 403
    created = harness.client.post("/api/v1/email/mailboxes", headers={**manager, "Idempotency-Key": "mb-1"}, json=body)
    assert created.status_code == 201
    replay = harness.client.post("/api/v1/email/mailboxes", headers={**manager, "Idempotency-Key": "mb-1"}, json=body)
    assert replay.json() == created.json()
    # The address belongs to one tenant platform-wide: tenant B cannot claim (and so cannot receive) it.
    _, manager_b = harness.user(harness.tenant_b, {"emails:campaigns:manage"})
    taken = harness.client.post(
        "/api/v1/email/mailboxes", headers=manager_b, json={"address": "HELP@acme.example.test"}
    )
    assert taken.status_code == 409
    for bad in ("not-an-address", "a@b@c.test", "Name <x@y.test>", "x@y.test, z@w.test"):
        assert harness.client.post("/api/v1/email/mailboxes", headers=manager, json={"address": bad}).status_code == 422
    agent_id = str(uuid.uuid4())
    voice_agent = str(uuid.uuid4())
    harness.fake.agents[agent_id] = {"agent_id": agent_id, "name": "Email", "kind": "email", "status": "active"}
    harness.fake.agents[voice_agent] = {"agent_id": voice_agent, "name": "Voice", "kind": "voice", "status": "active"}
    mb = created.json()
    patch = {"agent_id": voice_agent, "expected_version": mb["version"]}
    assert harness.client.patch(f"/api/v1/email/mailboxes/{mb['id']}", headers=manager, json=patch).status_code == 422
    patch = {"agent_id": str(uuid.uuid4()), "expected_version": mb["version"]}
    assert harness.client.patch(f"/api/v1/email/mailboxes/{mb['id']}", headers=manager, json=patch).status_code == 422
    ok = harness.client.patch(
        f"/api/v1/email/mailboxes/{mb['id']}",
        headers=manager,
        json={"agent_id": agent_id, "expected_version": mb["version"]},
    )
    assert ok.status_code == 200 and ok.json()["version"] == mb["version"] + 1
    stale = harness.client.patch(
        f"/api/v1/email/mailboxes/{mb['id']}",
        headers=manager,
        json={"auto_draft": False, "expected_version": mb["version"]},
    )
    assert stale.status_code == 409
    assert (
        harness.client.patch(
            f"/api/v1/email/mailboxes/{mb['id']}", headers=manager_b, json={"auto_draft": False, "expected_version": 2}
        ).status_code
        == 404
    )
    assert harness.audits("email.mailbox.created", harness.tenant_a)


def test_sweeper_requeues_stuck_work(harness):
    mb = harness.mailbox(harness.tenant_a)
    assert harness.post_envelope(harness.envelope(to=[mb["address"]])).status_code == 200
    harness.jobs.clear()  # simulate a lost publish after commit
    old = datetime.now(UTC) - timedelta(hours=1)
    harness.sql("UPDATE provider_events SET updated_at = :t", {"t": old})
    counts = harness.run(sweep)
    assert counts["inbound"] == 1 and harness.jobs[0].name == "emails.inbound_process"
    harness.drain(names=("emails.inbound_process",))
    harness.jobs.clear()  # the draft job is lost too
    harness.sql("UPDATE drafts SET updated_at = :t", {"t": old}, tenant=harness.tenant_a)
    assert harness.run(sweep)["drafts"] == 1
    harness.drain()
    _, headers = harness.user(harness.tenant_a, APPROVE)
    draft = harness.only_draft(harness.tenant_a, headers)
    harness.client.post(
        f"/api/v1/email/drafts/{draft['id']}/approve",
        headers={**headers, "Idempotency-Key": "sw-1"},
        json={"version": 1},
    )
    harness.jobs.clear()
    # A worker crashed mid-send: `sending` goes back to the queue and is retried with the same key.
    harness.sql("UPDATE send_commands SET status='sending', updated_at=:t", {"t": old}, tenant=harness.tenant_a)
    harness.sql("UPDATE drafts SET status='sending', updated_at=:t", {"t": old}, tenant=harness.tenant_a)
    assert harness.run(sweep)["sends"] == 1
    assert harness.run_sends() == ["sent"] and harness.fake.delivered == 1
    assert harness.run(sweep) == {"inbound": 0, "drafts": 0, "sends": 0}


def test_retention_purges_content_and_files_but_keeps_metadata(harness):
    mb = harness.mailbox(harness.tenant_a)
    harness.deliver(
        mb["address"],
        attachments=[
            {"filename": "a.pdf", "content_type": "application/pdf", "content_b64": b64(b"%PDF-1.4")},
            {"filename": "e.txt", "content_type": "text/plain", "content_b64": b64(EICAR)},
        ],
    )
    old = datetime.now(UTC) - timedelta(days=400)
    harness.sql("UPDATE messages SET received_at = :t", {"t": old}, tenant=harness.tenant_a)
    harness.sql("UPDATE attachments SET created_at = :t", {"t": old}, tenant=harness.tenant_a)
    assert any(b == "attachments" for b, _ in harness.store.objects)
    totals = harness.run(retention)
    assert totals["messages"] == 1 and totals["attachments"] == 2
    msg = harness.sql(
        "SELECT text_body, html_sanitized, raw_object_key, content_purged_at FROM messages", tenant=harness.tenant_a
    )[0]
    assert msg["text_body"] == "" and msg["raw_object_key"] is None and msg["content_purged_at"] is not None
    assert not [k for k in harness.store.objects if k[0] in ("attachments", "raw-mail")]
    statuses = {r["scan_status"] for r in harness.sql("SELECT scan_status FROM attachments", tenant=harness.tenant_a)}
    assert statuses == {"purged"}
    assert harness.audits("email.retention.purged", harness.tenant_a)


def test_demo_seed_is_idempotent_and_never_approves(harness):
    harness.run(lambda deps: seed(deps.db))
    harness.run(lambda deps: seed(deps.db))
    from nexus_core.devseed import ACME, GLOBEX

    for tenant in (ACME.id, GLOBEX.id):
        drafts = harness.sql("SELECT status FROM drafts", tenant=tenant)
        assert [d["status"] for d in drafts] == ["pending_review"]
        assert len(harness.sql("SELECT id FROM draft_versions", tenant=tenant)) == 1
        assert harness.sql("SELECT id FROM send_commands", tenant=tenant) == []
