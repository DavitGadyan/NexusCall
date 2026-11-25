"""The core rule: no AI draft is ever sent without a persisted human approval, checked again at send time."""

from __future__ import annotations

import uuid

import pytest
from inbox_support import APPROVE, REVIEW, Job, draft_result
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _pending(harness, tenant=None, **deliver):
    tenant = tenant or harness.tenant_a
    mb = harness.mailbox(tenant)
    harness.deliver(mb["address"], headers={"Message-ID": f"<{uuid.uuid4().hex}@example.test>"}, **deliver)
    approver, headers = harness.user(tenant, APPROVE)
    draft = harness.only_draft(tenant, headers)
    assert draft["status"] == "pending_review" and draft["current_version"] == 1
    return mb, approver, headers, draft


def _approve(harness, draft_id, headers, version=1, key=None):
    return harness.client.post(
        f"/api/v1/email/drafts/{draft_id}/approve",
        headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex},
        json={"version": version},
    )


def test_approved_draft_is_sent_exactly_once_with_threading_headers(harness):
    mb, approver, headers, draft = _pending(harness)
    resp = _approve(harness, draft["id"], headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert harness.fake.send_requests == []  # approval only enqueues
    assert harness.run_sends() == ["sent"]
    assert harness.fake.delivered == 1
    sent = harness.fake.send_requests[0]
    assert sent["key"] == f"draft:{draft['id']}:v1"
    body = sent["body"]
    assert body["category"] == "reply" and body["from_address"] == mb["address"]
    assert body["to"] == "customer@example.test" and body["subject"] == "Re: Opening hours?"
    assert body["headers"]["In-Reply-To"].endswith("@example.test>")
    assert body["headers"]["References"].endswith(body["headers"]["In-Reply-To"])
    assert body["source"] == {"type": "email_draft", "id": draft["id"], "version": 1}
    assert body["text"].startswith("Hello,") and body["html"] is None
    assert sent["headers"]["x-tenant-id"] == str(harness.tenant_a)
    detail = harness.client.get(f"/api/v1/email/drafts/{draft['id']}", headers=headers).json()
    assert detail["status"] == "sent" and detail["send_commands"][0]["status"] == "sent"
    assert [m["direction"] for m in detail["thread_messages"]] == ["inbound", "outbound"]
    assert harness.events("email.reply_draft.approved", harness.tenant_a)
    assert harness.events("email.reply.sent", harness.tenant_a)
    assert harness.audits("email.draft.approved", harness.tenant_a)[0]["actor"]["id"] == approver
    # Running the task again (duplicate delivery) does nothing.
    job = Job("emails.send_approved", _ctx(harness), {"command_id": detail["send_commands"][0]["id"]})
    assert harness.run_job(job) == "skipped"
    assert harness.fake.delivered == 1 and len(harness.fake.send_requests) == 1


def _ctx(harness, tenant=None):
    from nexus_core.celery_app import JobContext

    return JobContext(tenant_id=tenant or harness.tenant_a, correlation_id=uuid.uuid4())


def test_approval_requires_permission_step_up_current_version_and_idempotency_key(harness):
    _, _, approver_headers, draft = _pending(harness)
    reviewer, reviewer_headers = harness.user(harness.tenant_a, REVIEW)
    assert _approve(harness, draft["id"], reviewer_headers).status_code == 403
    approver, _ = harness.user(harness.tenant_a, APPROVE)
    no_mfa = harness.headers_for(approver, harness.tenant_a, mfa=False)
    resp = _approve(harness, draft["id"], no_mfa)
    assert resp.status_code == 401 and resp.json()["code"] == "step_up_required"
    assert _approve(harness, draft["id"], approver_headers, version=2).status_code == 409
    assert _approve(harness, draft["id"], approver_headers, version=0).status_code == 422
    missing_key = harness.client.post(
        f"/api/v1/email/drafts/{draft['id']}/approve", headers=approver_headers, json={"version": 1}
    )
    assert missing_key.status_code == 422
    extra = harness.client.post(
        f"/api/v1/email/drafts/{draft['id']}/approve",
        headers={**approver_headers, "Idempotency-Key": "k-extra"},
        json={"version": 1, "to": "attacker@evil.example", "text": "hijacked"},
    )
    assert extra.status_code == 422
    service = harness.tokens.service("svc-ai-agent-service")
    from nexus_core.testing import auth

    assert _approve(harness, draft["id"], auth(service, harness.tenant_a)).status_code == 403
    assert harness.sql("SELECT id FROM send_commands", tenant=harness.tenant_a) == []
    assert harness.send_jobs() == []


def test_double_approve_and_replays_produce_one_send(harness):
    _, _, headers, draft = _pending(harness)
    first = _approve(harness, draft["id"], headers, key="approve-1")
    replay = _approve(harness, draft["id"], headers, key="approve-1")
    assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
    mismatch = harness.client.post(
        f"/api/v1/email/drafts/{draft['id']}/approve",
        headers={**headers, "Idempotency-Key": "approve-1"},
        json={"version": 2},
    )
    assert mismatch.status_code == 422
    other_key = _approve(harness, draft["id"], headers, key="approve-2")
    assert other_key.status_code == 409
    # A second approver with a different key cannot create a second command either.
    _, other_headers = harness.user(harness.tenant_a, APPROVE)
    assert _approve(harness, draft["id"], other_headers).status_code == 409
    assert len(harness.sql("SELECT id FROM send_commands", tenant=harness.tenant_a)) == 1
    # Even if the job is enqueued several times, one email goes out.
    jobs = harness.send_jobs()
    harness.jobs.extend([jobs[0], jobs[0]])
    assert sorted(harness.run_sends()) == ["sent", "skipped", "skipped"]
    assert harness.fake.delivered == 1 and len(harness.fake.send_requests) == 1


def test_approver_who_loses_permission_before_the_send_sends_nothing(harness):
    _, approver, headers, draft = _pending(harness)
    assert _approve(harness, draft["id"], headers).status_code == 200
    harness.authz.grant(approver, harness.tenant_a, REVIEW)  # demoted: approve permission removed
    assert harness.run_sends() == ["blocked"]
    assert harness.fake.send_requests == []
    assert _approve(harness, draft["id"], headers).status_code == 403  # also refused at the API immediately
    _, boss = harness.user(harness.tenant_a, APPROVE)
    detail = harness.client.get(f"/api/v1/email/drafts/{draft['id']}", headers=boss).json()
    assert (
        detail["status"] == "pending_review" and detail["send_commands"][0]["last_error"] == "approver_not_authorized"
    )
    assert harness.audits("email.reply.send_blocked", harness.tenant_a)
    # Membership removed entirely behaves the same; a still-authorized approver can re-approve.
    assert _approve(harness, draft["id"], boss).status_code == 200
    assert harness.run_sends() == ["sent"] and harness.fake.delivered == 1


def test_edit_after_approval_requires_a_new_approval(harness):
    _, _, headers, draft = _pending(harness)
    assert _approve(harness, draft["id"], headers).status_code == 200
    edit = harness.client.put(
        f"/api/v1/email/drafts/{draft['id']}",
        headers=headers,
        json={"text": "Edited by a human after approval.", "expected_version": 1},
    )
    assert edit.status_code == 200, edit.text
    body = edit.json()
    assert body["status"] == "pending_review" and body["current_version"] == 2 and body["approved_version"] is None
    assert [v["author_type"] for v in body["versions"]] == ["ai", "user"]
    assert body["versions"][0]["ai_generated"] is True and body["versions"][1]["ai_generated"] is False
    # The v1 command was enqueued before the edit: running it now sends nothing.
    assert harness.run_sends() == ["skipped"]
    assert harness.fake.send_requests == []
    assert _approve(harness, draft["id"], headers, version=1).status_code == 409  # stale version
    assert _approve(harness, draft["id"], headers, version=2).status_code == 200
    assert harness.run_sends() == ["sent"]
    assert harness.fake.send_requests[0]["body"]["text"] == "Edited by a human after approval."
    assert harness.fake.send_requests[0]["key"] == f"draft:{draft['id']}:v2"
    stale = harness.client.put(
        f"/api/v1/email/drafts/{draft['id']}", headers=headers, json={"text": "too late", "expected_version": 2}
    )
    assert stale.status_code == 409  # sent drafts are final


def test_send_task_run_directly_never_sends_an_unapproved_or_forged_draft(harness):
    _, approver, headers, draft = _pending(harness)
    tenant = harness.tenant_a
    cmd_id = uuid.uuid4()
    # Forged state written straight into the database (no approval action exists).
    harness.sql(
        "UPDATE drafts SET status='approved', approved_version=1, approved_by=:a WHERE id=:d",
        {"a": approver, "d": draft["id"]},
        tenant=tenant,
    )
    harness.sql(
        "INSERT INTO send_commands (id, tenant_id, draft_id, version, approved_by, from_address, to_address, "
        "subject, idempotency_key) VALUES (:i, :t, :d, 1, :a, 'x@example.test', 'attacker@evil.example', 's', 'k')",
        {"i": cmd_id, "t": tenant, "d": draft["id"], "a": approver},
        tenant=tenant,
    )
    assert harness.run_job(Job("emails.send_approved", _ctx(harness), {"command_id": str(cmd_id)})) == "blocked"
    assert harness.fake.send_requests == []
    row = harness.sql("SELECT status, last_error FROM send_commands WHERE id=:i", {"i": cmd_id}, tenant=tenant)[0]
    assert row["status"] == "blocked" and row["last_error"] == "approval_missing"
    # A command whose draft is not approved at that version is canceled, not sent.
    _, _, headers2, draft2 = _pending(harness, tenant=harness.tenant_b)
    cmd2 = uuid.uuid4()
    harness.sql(
        "INSERT INTO send_commands (id, tenant_id, draft_id, version, approved_by, from_address, to_address, "
        "subject, idempotency_key) VALUES (:i, :t, :d, 1, :a, 'x@example.test', 'y@example.test', 's', 'k2')",
        {"i": cmd2, "t": harness.tenant_b, "d": draft2["id"], "a": approver},
        tenant=harness.tenant_b,
    )
    assert (
        harness.run_job(Job("emails.send_approved", _ctx(harness, harness.tenant_b), {"command_id": str(cmd2)}))
        == "canceled"
    )
    # A job for tenant A cannot reach tenant B's command (RLS: not found -> skipped).
    assert harness.run_job(Job("emails.send_approved", _ctx(harness), {"command_id": str(cmd2)})) == "skipped"
    assert harness.fake.send_requests == []


def test_send_endpoint_only_retries_a_failed_approved_send(harness):
    _, _, headers, draft = _pending(harness)
    url = f"/api/v1/email/drafts/{draft['id']}/send"
    not_failed = harness.client.post(url, headers={**headers, "Idempotency-Key": "s1"}, json={"version": 1})
    assert not_failed.status_code == 409  # pending_review: never sendable through /send
    free_form = harness.client.post(
        url, headers={**headers, "Idempotency-Key": "s2"}, json={"version": 1, "to": "x@evil.example", "text": "hi"}
    )
    assert free_form.status_code == 422
    assert _approve(harness, draft["id"], headers).status_code == 200
    harness.fake.send_status = 422  # email-service rejects (e.g. unverified sender domain)
    assert harness.run_sends() == ["failed"]
    detail = harness.client.get(f"/api/v1/email/drafts/{draft['id']}", headers=headers).json()
    assert detail["status"] == "send_failed"
    _, reviewer = harness.user(harness.tenant_a, REVIEW)
    assert (
        harness.client.post(url, headers={**reviewer, "Idempotency-Key": "s3"}, json={"version": 1}).status_code == 403
    )
    assert (
        harness.client.post(url, headers={**headers, "Idempotency-Key": "s4"}, json={"version": 2}).status_code == 409
    )
    harness.fake.send_status = 202
    retry = harness.client.post(url, headers={**headers, "Idempotency-Key": "s5"}, json={"version": 1})
    assert retry.status_code == 202, retry.text
    assert harness.run_sends() == ["sent"]
    assert harness.fake.delivered == 1
    assert {r["key"] for r in harness.fake.send_requests} == {f"draft:{draft['id']}:v1"}


def test_transient_failure_retries_with_the_same_idempotency_key(harness):
    from nexus_core.celery_app import TransientFailure

    _, _, headers, draft = _pending(harness)
    assert _approve(harness, draft["id"], headers).status_code == 200
    job = harness.send_jobs()[0]
    harness.fake.send_status = 503
    with pytest.raises(TransientFailure):
        harness.run_job(job)
    row = harness.sql("SELECT status, attempts FROM send_commands", tenant=harness.tenant_a)[0]
    assert row["status"] == "queued" and row["attempts"] == 1
    harness.fake.send_status = 202
    assert harness.run_job(job) == "sent"
    assert harness.run_job(job) == "skipped"
    keys = [r["key"] for r in harness.fake.send_requests]
    assert keys == [f"draft:{draft['id']}:v1"] * 2 and harness.fake.delivered == 1


def test_kill_switch_and_recipient_suppression_are_rechecked_at_send_time(harness):
    _, _, headers, draft = _pending(harness)
    assert _approve(harness, draft["id"], headers).status_code == 200
    harness.fake.emails_enabled = False
    assert harness.run_sends() == ["failed"]
    assert harness.fake.send_requests == []
    harness.fake.emails_enabled = True
    harness.fake.suppressed.add("customer@example.test")  # opted out after approval
    retry = harness.client.post(
        f"/api/v1/email/drafts/{draft['id']}/send", headers={**headers, "Idempotency-Key": "r1"}, json={"version": 1}
    )
    assert retry.status_code == 202
    assert harness.run_sends() == ["blocked"]
    assert harness.fake.send_requests == []


def test_authz_unavailable_fails_closed(harness):
    from nexus_core.celery_app import TransientFailure

    _, _, headers, draft = _pending(harness)
    assert _approve(harness, draft["id"], headers).status_code == 200
    harness.fake.authz_down = True
    with pytest.raises(TransientFailure):
        harness.run_job(harness.send_jobs()[0])
    assert harness.fake.send_requests == []
    assert harness.sql("SELECT status FROM send_commands", tenant=harness.tenant_a)[0]["status"] == "queued"


def test_review_actions_reject_assign_escalate_regenerate(harness):
    _, _, headers, draft = _pending(harness)
    reviewer, reviewer_headers = harness.user(harness.tenant_a, REVIEW)
    did = draft["id"]
    bad_assign = harness.client.post(
        f"/api/v1/email/drafts/{did}/assign", headers=reviewer_headers, json={"assignee": str(uuid.uuid4())}
    )
    assert bad_assign.status_code == 422  # not a reviewer in this tenant
    ok = harness.client.post(
        f"/api/v1/email/drafts/{did}/assign", headers=reviewer_headers, json={"assignee": reviewer}
    )
    assert ok.status_code == 200 and ok.json()["assigned_to"] == reviewer
    esc = harness.client.post(
        f"/api/v1/email/drafts/{did}/escalate", headers=reviewer_headers, json={"reason": "legal"}
    )
    assert esc.status_code == 200 and esc.json()["escalated"] is True
    assert harness.events("notification.requested", harness.tenant_a)
    regen = harness.client.post(f"/api/v1/email/drafts/{did}/regenerate", headers=reviewer_headers)
    assert regen.status_code == 202 and regen.json()["status"] == "queued"
    harness.drain()
    detail = harness.client.get(f"/api/v1/email/drafts/{did}", headers=reviewer_headers).json()
    assert detail["current_version"] == 2 and detail["status"] == "pending_review"
    rej = harness.client.post(
        f"/api/v1/email/drafts/{did}/reject", headers=reviewer_headers, json={"reason": "inaccurate"}
    )
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    assert harness.events("email.reply_draft.rejected", harness.tenant_a)
    assert _approve(harness, did, headers, version=2).status_code == 409
    actions = [
        a["action"] for a in harness.client.get(f"/api/v1/email/drafts/{did}", headers=headers).json()["actions"]
    ]
    assert actions == ["requested", "generated", "assigned", "escalated", "regenerated", "generated", "rejected"]
    _, nobody = harness.user(harness.tenant_a, {"emails:inbox:read"})
    for path in ("", "/regenerate", "/reject", "/assign", "/escalate"):
        method = harness.client.get if path == "" else harness.client.post
        kwargs = {} if path in ("", "/regenerate") else {"json": {}}
        assert method(f"/api/v1/email/drafts/{did}{path}", headers=nobody, **kwargs).status_code == 403


def test_sensitive_intents_are_escalated_and_drafting_failure_leaves_thread_for_humans(harness):
    harness.fake.draft = lambda body: {**_base(), "intent": "legal", "sensitive": True}
    _, _, headers, draft = _pending(harness)
    assert draft["escalated"] is True and draft["escalation_reason"] == "intent_legal"
    harness.fake.drafting_status = 403  # AI disabled by policy
    mb = harness.mailbox(harness.tenant_a)
    harness.deliver(mb["address"], headers={"Message-ID": "<fail@example.test>"})
    failed = [
        d
        for d in harness.client.get("/api/v1/email/drafts", headers=headers).json()["items"]
        if d["status"] == "generation_failed"
    ]
    assert len(failed) == 1 and failed[0]["last_error"] == "policy_denied"
    threads = harness.client.get("/api/v1/email/inbox/threads", headers=headers).json()["items"]
    assert any(t["status"] == "open" for t in threads)
    # A human can still write the reply from scratch (version 1, author user) and approve it.
    edit = harness.client.put(
        f"/api/v1/email/drafts/{failed[0]['id']}",
        headers=headers,
        json={"text": "Written by a person.", "expected_version": 0},
    )
    assert edit.status_code == 200 and edit.json()["versions"][0]["author_type"] == "user"


def _base():
    return draft_result({})


def test_append_only_history_cannot_be_changed(harness):
    _, _, headers, draft = _pending(harness)
    for statement in (
        "UPDATE draft_versions SET body_text = 'tampered'",
        "DELETE FROM draft_versions",
        "UPDATE draft_actions SET actor_id = 'someone-else'",
        "DELETE FROM draft_actions",
    ):
        with pytest.raises(DBAPIError):
            harness.sql(statement, tenant=harness.tenant_a)
    versions = harness.client.get(f"/api/v1/email/drafts/{draft['id']}", headers=headers).json()["versions"]
    assert versions[0]["body_text"].startswith("Hello,")


def test_cross_tenant_isolation_for_every_resource(harness):
    _, _, headers_a, draft = _pending(harness)
    _, headers_b = harness.user(harness.tenant_b, APPROVE | {"emails:campaigns:manage"})
    thread_id = draft["thread_id"]
    att = harness.sql("SELECT id FROM messages", tenant=harness.tenant_a)[0]["id"]
    assert harness.client.get(f"/api/v1/email/drafts/{draft['id']}", headers=headers_b).status_code == 404
    assert harness.client.get(f"/api/v1/email/inbox/threads/{thread_id}", headers=headers_b).status_code == 404
    assert harness.client.get(f"/api/v1/email/inbox/attachments/{att}/download", headers=headers_b).status_code == 404
    assert (
        harness.client.put(
            f"/api/v1/email/drafts/{draft['id']}", headers=headers_b, json={"text": "x", "expected_version": 1}
        ).status_code
        == 404
    )
    assert _approve(harness, draft["id"], headers_b).status_code == 404
    assert (
        harness.client.post(f"/api/v1/email/drafts/{draft['id']}/reject", headers=headers_b, json={}).status_code == 404
    )
    assert harness.client.get("/api/v1/email/drafts", headers=headers_b).json()["items"] == []
    assert harness.client.get("/api/v1/email/mailboxes", headers=headers_b).json()["items"] == []
    # A user of tenant A cannot select tenant B (no membership) at all.
    a_user, _ = harness.user(harness.tenant_a, APPROVE)
    assert (
        harness.client.get("/api/v1/email/drafts", headers=harness.headers_for(a_user, harness.tenant_b)).status_code
        == 403
    )
    # RLS: raw SQL in tenant B's context sees none of tenant A's rows; no context sees nothing.
    for table in (
        "mailboxes",
        "threads",
        "messages",
        "attachments",
        "drafts",
        "draft_versions",
        "draft_actions",
        "send_commands",
    ):
        assert harness.sql(f"SELECT count(*) AS n FROM {table}", tenant=harness.tenant_b)[0]["n"] == 0, table
        assert harness.sql(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0, table
    with pytest.raises(DBAPIError):
        harness.sql("UPDATE drafts SET tenant_id = :b", {"b": harness.tenant_b}, tenant=harness.tenant_a)


def test_draft_provenance_and_transient_drafting_failures(harness):
    from nexus_core.celery_app import TransientFailure

    _, _, headers, draft = _pending(harness)
    detail = harness.client.get(f"/api/v1/email/drafts/{draft['id']}", headers=headers).json()
    v1 = detail["versions"][0]
    assert v1["model"] == "claude-sonnet-5-5" and v1["provider"] == "fake" and v1["prompt_version"] == 3
    assert v1["citations"][0]["source_name"] == "Opening hours FAQ" and v1["quality"]["confidence"] == 0.82
    assert detail["original_message"]["id"] == draft["message_id"]
    gen = harness.audits("email.draft.generated", harness.tenant_a)[0]["payload"]["metadata"]
    assert "draft_text" not in gen and "body" not in gen  # audit carries ids and enums, never content
    # crm unavailable during drafting: the draft goes back to the queue (fail closed), nothing is generated.
    mb = harness.mailbox(harness.tenant_a)
    harness.fake.crm_down = True
    envelope = harness.envelope(to=[mb["address"]], headers={"Message-ID": "<crm-down@example.test>"})
    assert harness.post_envelope(envelope).status_code == 200
    harness.drain(names=("emails.inbound_process",))
    job = next(j for j in harness.jobs if j.name == "emails.ai_draft")
    with pytest.raises(TransientFailure):
        harness.run_job(job)
    status = harness.sql("SELECT status FROM drafts WHERE id = :i", {"i": job.ids["draft_id"]}, tenant=harness.tenant_a)
    assert status[0]["status"] == "queued"
    harness.fake.crm_down = False
    assert harness.run_job(job) == "generated"
    assert harness.run_job(job) == "skipped"  # duplicate delivery of the same job
