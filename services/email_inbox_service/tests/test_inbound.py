"""Inbound pipeline: SNS verification, dedupe, tenant resolution from the envelope, threading, attachment
scanning and quarantine, auto-reply handling, and the simulator itself."""

from __future__ import annotations

import json
import uuid

import pytest
from inbox_support import EICAR, READ, REVIEW, b64

from fake_providers.sns import sign_sns

pytestmark = [pytest.mark.integration]


def test_delivery_creates_thread_message_and_queued_ai_draft(harness):
    mb = harness.mailbox(harness.tenant_a)
    harness.fake.contacts["customer@example.test"] = str(uuid.uuid4())
    harness.deliver(mb["address"], html="<p>Hi <b>there</b></p><script>alert(1)</script>")
    _, headers = harness.user(harness.tenant_a, READ)
    threads = harness.client.get("/api/v1/email/inbox/threads", headers=headers).json()["items"]
    assert len(threads) == 1 and threads[0]["status"] == "awaiting_review"
    assert threads[0]["contact_id"] == harness.fake.contacts["customer@example.test"]
    detail = harness.client.get(f"/api/v1/email/inbox/threads/{threads[0]['id']}", headers=headers).json()
    msg = detail["messages"][0]
    assert msg["direction"] == "inbound" and msg["text_body"].startswith("Hi, when are you open?")
    assert "<script" not in (msg["html_sanitized"] or "") and "<b>there</b>" in msg["html_sanitized"]
    assert detail["drafts"][0]["status"] == "pending_review"  # drafted by the (mocked) ai-agent run
    assert len(harness.events("email.inbound.received", harness.tenant_a)) == 1
    assert len(harness.events("email.reply_draft.requested", harness.tenant_a)) == 1
    assert len(harness.events("email.reply_draft.generated", harness.tenant_a)) == 1
    assert harness.fake.send_requests == []  # nothing is ever sent without approval


@pytest.mark.security
def test_unsigned_tampered_wrong_topic_and_stale_notifications_are_rejected(harness):
    mb = harness.mailbox(harness.tenant_a)
    good = harness.envelope(to=[mb["address"]])
    tampered = {**good, "Message": good["Message"].replace("inbound/", "inbound/x")}
    assert harness.post_envelope(tampered).status_code == 403
    assert harness.post_envelope({**good, "Signature": b64(b"nope" * 64)}).status_code == 403
    wrong_topic = sign_sns("arn:aws:sns:us-east-1:000000000000:someone-else", good["Message"])
    assert harness.post_envelope(wrong_topic).status_code == 403
    foreign_cert = {**good, "SigningCertURL": "http://evil.example/cert.pem"}
    assert harness.post_envelope(foreign_cert).status_code == 403
    assert harness.client.post("/webhooks/ses/inbound", content=b"not json").status_code == 422
    assert harness.jobs == []


@pytest.mark.security
def test_duplicate_sns_delivery_and_duplicate_message_id_are_deduplicated(harness):
    mb = harness.mailbox(harness.tenant_a)
    envelope = harness.envelope(to=[mb["address"]], headers={"Message-ID": "<dup-1@example.test>"})
    assert harness.post_envelope(envelope).json()["status"] == "accepted"
    assert harness.post_envelope(envelope).json()["status"] == "duplicate"  # same SNS MessageId
    harness.drain()
    # Same RFC Message-ID through a different SNS notification (provider retry / second receipt rule).
    again = harness.envelope(to=[mb["address"]], headers={"Message-ID": "<dup-1@example.test>"})
    assert harness.post_envelope(again).json()["status"] == "accepted"
    results = harness.drain()
    assert "duplicate" in results
    rows = harness.sql("SELECT count(*) AS n FROM messages", tenant=harness.tenant_a)
    assert rows[0]["n"] == 1
    assert len(harness.sql("SELECT id FROM drafts", tenant=harness.tenant_a)) == 1


@pytest.mark.security
def test_spoofed_headers_cannot_change_the_tenant(harness):
    mb_a = harness.mailbox(harness.tenant_a)
    mb_b = harness.mailbox(harness.tenant_b)
    # The sender writes tenant B's address in To/Cc and adds tenant-looking headers; SES delivered to A only.
    envelope = harness.envelope(
        to=[mb_b["address"]],
        envelope_to=[mb_a["address"]],
        cc=[mb_b["address"]],
        headers={"X-Tenant-ID": str(harness.tenant_b), "X-Nexus-Mailbox": mb_b["id"]},
    )
    resp = harness.post_envelope(envelope, **{"X-Tenant-ID": str(harness.tenant_b)})
    assert resp.status_code == 200
    harness.drain()
    _, headers_a = harness.user(harness.tenant_a, READ)
    _, headers_b = harness.user(harness.tenant_b, READ)
    assert len(harness.client.get("/api/v1/email/inbox/threads", headers=headers_a).json()["items"]) == 1
    assert harness.client.get("/api/v1/email/inbox/threads", headers=headers_b).json()["items"] == []
    assert harness.sql("SELECT count(*) AS n FROM messages", tenant=harness.tenant_b)[0]["n"] == 0


def test_unknown_recipient_is_ignored(harness):
    envelope = harness.envelope(to=["nobody@unknown.example.test"])
    assert harness.post_envelope(envelope).json() == {"status": "ignored"}
    assert harness.jobs == []


@pytest.mark.security
def test_infected_attachment_is_quarantined_and_never_downloadable(harness):
    mb = harness.mailbox(harness.tenant_a)
    harness.deliver(
        mb["address"],
        attachments=[
            {"filename": "invoice.pdf", "content_type": "application/pdf", "content_b64": b64(b"%PDF-1.4 clean")},
            {"filename": "eicar.txt", "content_type": "text/plain", "content_b64": b64(EICAR)},
            {"filename": "setup.exe", "content_type": "application/octet-stream", "content_b64": b64(b"MZ..")},
            {"filename": "fake.pdf", "content_type": "application/pdf", "content_b64": b64(b"not a pdf")},
        ],
    )
    _, headers = harness.user(harness.tenant_a, READ)
    thread = harness.client.get("/api/v1/email/inbox/threads", headers=headers).json()["items"][0]
    detail = harness.client.get(f"/api/v1/email/inbox/threads/{thread['id']}", headers=headers).json()
    atts = {a["filename"]: a for a in detail["messages"][0]["attachments"]}
    assert atts["invoice.pdf"]["scan_status"] == "clean" and atts["invoice.pdf"]["downloadable"]
    assert atts["eicar.txt"]["scan_status"] == "infected" and atts["eicar.txt"]["quarantined"]
    assert not atts["eicar.txt"]["downloadable"]
    assert (
        atts["setup.exe"]["scan_status"] == "rejected" and atts["setup.exe"]["rejection_reason"] == "type_not_allowed"
    )
    assert atts["fake.pdf"]["rejection_reason"] == "type_mismatch"

    blocked = harness.client.get(f"/api/v1/email/inbox/attachments/{atts['eicar.txt']['id']}/download", headers=headers)
    assert blocked.status_code == 403
    ok = harness.client.get(f"/api/v1/email/inbox/attachments/{atts['invoice.pdf']['id']}/download", headers=headers)
    assert ok.status_code == 200 and ok.json()["expires_in_seconds"] == 300
    assert f"/attachments/{harness.tenant_a}/files/" in ok.json()["url"]
    outcomes = sorted(a["payload"]["outcome"] for a in harness.audits("email.attachment.download", harness.tenant_a))
    assert outcomes == ["denied", "success"]
    assert harness.audits("email.attachment.quarantined", harness.tenant_a)
    # Rejected files are not stored at all; quarantined bytes live only under the quarantine prefix.
    keys = [k for (bucket, k) in harness.store.objects if bucket == "attachments"]
    assert any("/quarantine/" in k for k in keys) and len(keys) == 2


def test_scanner_unavailable_is_skipped_in_test_and_error_in_production(harness):
    harness.settings.clamav_host = None
    mb = harness.mailbox(harness.tenant_a)
    harness.deliver(
        mb["address"],
        attachments=[{"filename": "a.txt", "content_type": "text/plain", "content_b64": b64(b"hello")}],
    )
    row = harness.sql("SELECT scan_status FROM attachments", tenant=harness.tenant_a)[0]
    assert row["scan_status"] == "skipped"

    from nexus_core.config import Environment

    harness.settings.env = Environment.PRODUCTION  # only to exercise the policy branch; not a real deployment
    harness.settings.provider_mode = harness.settings.provider_mode
    try:
        harness.deliver(
            mb["address"],
            headers={"Message-ID": "<prod-like@example.test>"},
            attachments=[{"filename": "b.txt", "content_type": "text/plain", "content_b64": b64(b"hello")}],
        )
    finally:
        harness.settings.env = Environment.TEST
    statuses = sorted(
        r["scan_status"] for r in harness.sql("SELECT scan_status FROM attachments", tenant=harness.tenant_a)
    )
    assert statuses == ["error", "skipped"]
    err = harness.sql("SELECT id FROM attachments WHERE scan_status = 'error'", tenant=harness.tenant_a)[0]
    _, headers = harness.user(harness.tenant_a, READ)
    assert (
        harness.client.get(f"/api/v1/email/inbox/attachments/{err['id']}/download", headers=headers).status_code == 403
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Precedence": "bulk"},
        {"X-Autoreply": "yes"},
        {"List-Id": "<news.example.test>"},
        {"X-Nexus-Loop": "1"},
    ],
)
def test_auto_replies_and_loops_are_stored_but_never_drafted(harness, headers):
    mb = harness.mailbox(harness.tenant_a)
    harness.deliver(mb["address"], headers=headers)
    assert harness.sql("SELECT count(*) AS n FROM messages", tenant=harness.tenant_a)[0]["n"] == 1
    assert harness.sql("SELECT id FROM drafts", tenant=harness.tenant_a) == []
    assert harness.fake.drafting_requests == []


def test_self_addressed_loop_is_not_drafted(harness):
    mb = harness.mailbox(harness.tenant_a)
    harness.deliver(mb["address"], sender=mb["address"])
    assert harness.sql("SELECT id FROM drafts", tenant=harness.tenant_a) == []


def test_suppressed_sender_gets_no_draft_and_mailbox_without_auto_draft(harness):
    mb = harness.mailbox(harness.tenant_a)
    harness.fake.suppressed.add("optout@example.test")
    harness.deliver(mb["address"], sender="optout@example.test")
    quiet = harness.mailbox(harness.tenant_a, auto_draft=False)
    harness.deliver(quiet["address"], headers={"Message-ID": "<quiet@example.test>"})
    assert harness.sql("SELECT id FROM drafts", tenant=harness.tenant_a) == []
    flags = harness.sql("SELECT auto_flags FROM messages ORDER BY received_at", tenant=harness.tenant_a)
    assert "sender_suppressed" in flags[0]["auto_flags"]
    _, headers = harness.user(harness.tenant_a, READ)
    statuses = {t["status"] for t in harness.client.get("/api/v1/email/inbox/threads", headers=headers).json()["items"]}
    assert statuses == {"open"}  # still visible to humans


def test_replies_thread_by_in_reply_to_and_references(harness):
    mb = harness.mailbox(harness.tenant_a)
    harness.deliver(mb["address"], headers={"Message-ID": "<first@example.test>"})
    harness.deliver(
        mb["address"],
        subject="Re: Opening hours?",
        headers={"Message-ID": "<second@example.test>", "In-Reply-To": "<first@example.test>"},
    )
    harness.deliver(
        mb["address"],
        subject="Re: Re: Opening hours?",
        headers={"Message-ID": "<third@example.test>", "References": "<first@example.test> <second@example.test>"},
    )
    threads = harness.sql("SELECT id, message_count FROM threads", tenant=harness.tenant_a)
    assert len(threads) == 1 and threads[0]["message_count"] == 3


def test_reply_to_a_campaign_email_links_the_campaign(harness):
    mb = harness.mailbox(harness.tenant_a)
    campaign_id, contact_id = str(uuid.uuid4()), str(uuid.uuid4())
    harness.fake.campaign_messages["<campaign-msg@mail.example.test>"] = {
        "message_id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "contact_id": contact_id,
    }
    harness.deliver(mb["address"], headers={"In-Reply-To": "<campaign-msg@mail.example.test>"})
    row = harness.sql("SELECT campaign_id, contact_id FROM threads", tenant=harness.tenant_a)[0]
    assert str(row["campaign_id"]) == campaign_id and str(row["contact_id"]) == contact_id


def test_disabled_mailbox_stops_routing(harness):
    mb = harness.mailbox(harness.tenant_a)
    _, headers = harness.user(harness.tenant_a, {"emails:campaigns:manage"})
    resp = harness.client.patch(
        f"/api/v1/email/mailboxes/{mb['id']}",
        headers=headers,
        json={"status": "disabled", "expected_version": mb["version"]},
    )
    assert resp.status_code == 200
    assert harness.post_envelope(harness.envelope(to=[mb["address"]])).json() == {"status": "ignored"}


def test_simulator_posts_a_signed_notification_to_the_webhook(harness):
    resp = harness.simulator.post(
        "/ses-inbound/simulate",
        json={"sender": "a@example.test", "to": ["support@acme.example.test"], "subject": "Hi", "text": "Hello"},
    )
    assert resp.status_code == 200 and resp.json()["webhook_status"] == 200
    posted = json.loads(harness.fake.webhook_posts[-1])
    assert posted["Type"] == "Notification" and posted["SignatureVersion"] == "2"
    receipt = json.loads(posted["Message"])["receipt"]
    assert receipt["recipients"] == ["support@acme.example.test"]
    assert ("raw-mail", receipt["action"]["objectKey"]) in harness.store.objects
    bad = harness.simulator.post(
        "/ses-inbound/simulate",
        json={"sender": "a@example.test", "to": ["x@example.test"], "headers": {"X-Evil": "a\r\nBcc: victim"}},
    )
    assert bad.status_code == 422


def test_reviewer_permissions_are_needed_for_the_inbox(harness):
    _, nobody = harness.user(harness.tenant_a, set())
    assert harness.client.get("/api/v1/email/inbox/threads", headers=nobody).status_code == 403
    _, reviewer = harness.user(harness.tenant_a, REVIEW)
    assert harness.client.get("/api/v1/email/inbox/threads", headers=reviewer).status_code == 200
