"""SES/SNS webhooks, one-click unsubscribe, suppression sync and auto-pause, against a real database."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from email_service.jobs import scheduler_tick, sync_tenant_suppressions
from email_service.settings import FAKE_SES_EVENTS_TOPIC
from fake_providers.sns import _material, sign_sns
from nexus_core.config import ProviderMode
from nexus_core.webhooks import sns_string_to_sign

from .conftest import ses_event

pytestmark = [pytest.mark.integration]


def _sent_message(harness, tenant, to="ada@example.test", category="transactional"):
    domain = harness.domain(tenant)
    harness.enqueued.clear()
    r = harness.internal_send(
        tenant,
        {
            "category": category,
            "from_address": f"support@{domain['domain']}",
            "to": to,
            "subject": "Hi",
            "text": "Hello",
            "source": {"type": "draft", "id": "d1"},
        },  # fmt: skip
    )
    assert r.status_code == 202, r.text
    message_id = r.json()["message_id"]
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "accepted"
    return message_id, outcome.detail


# ------------------------------------------------------------------------------------------------- webhooks
@pytest.mark.security
def test_delivery_event_is_applied_once(harness, tenants):
    tenant, _ = tenants
    message_id, provider_id = _sent_message(harness, tenant)
    envelope = harness.sns(ses_event("Delivery", provider_id, "ada@example.test"))
    r = harness.post_sns(envelope)
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert harness.message(message_id, tenant)["state"] == "delivered"
    again = harness.post_sns(envelope)  # SNS redelivery
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    ledger = harness.sql("SELECT count(*) AS n FROM provider_events WHERE message_id = :m", {"m": message_id},
                         tenant_id=tenant)  # fmt: skip
    assert ledger[0]["n"] == 1
    delivered = [e for e in harness.events("email.message.state_changed")
                 if e["payload"]["message_id"] == message_id and e["payload"]["state"] == "delivered"]  # fmt: skip
    assert len(delivered) == 1


@pytest.mark.security
def test_forged_tampered_and_wrong_topic_messages_are_rejected(harness, tenants):
    tenant, _ = tenants
    message_id, provider_id = _sent_message(harness, tenant)
    good = harness.sns(ses_event("Bounce", provider_id, "ada@example.test"))
    tampered = {**good, "Message": json.dumps(ses_event("Complaint", provider_id, "ada@example.test"))}
    assert harness.post_sns(tampered).status_code == 403
    bad_sig = {**good, "Signature": base64.b64encode(b"x" * 256).decode()}
    assert harness.post_sns(bad_sig).status_code == 403
    wrong_topic = harness.sns(ses_event("Bounce", provider_id, "ada@example.test"),
                              topic="arn:aws:sns:us-east-1:000000000000:someone-else")  # fmt: skip
    assert harness.post_sns(wrong_topic).status_code == 403
    evil_cert = {**good, "SigningCertURL": "https://sns.us-east-1.amazonaws.com.evil.test/cert.pem"}
    assert harness.post_sns(evil_cert).status_code == 403
    unsigned = {k: v for k, v in good.items() if k != "Signature"}
    assert harness.post_sns(unsigned).status_code == 403
    assert harness.client.post("/webhooks/ses/events", content=b"not json").status_code == 400
    assert harness.message(message_id, tenant)["state"] == "accepted"  # nothing was applied


def test_real_mode_without_topic_allowlist_fails_closed(harness, tenants):
    tenant, _ = tenants
    _, provider_id = _sent_message(harness, tenant)
    harness.settings.provider_mode = ProviderMode.REAL
    try:
        r = harness.post_sns(harness.sns(ses_event("Delivery", provider_id, "ada@example.test")))
    finally:
        harness.settings.provider_mode = ProviderMode.FAKE
    assert r.status_code == 403 and r.json()["code"] == "not_configured"


def _subscription(subscribe_url: str) -> dict[str, str]:
    envelope = sign_sns(FAKE_SES_EVENTS_TOPIC, "confirm", message_type="SubscriptionConfirmation")
    envelope.pop("Signature")
    envelope["Token"] = hashlib.sha256(envelope["MessageId"].encode()).hexdigest()
    envelope["SubscribeURL"] = subscribe_url
    signature = _material()[0].sign(sns_string_to_sign(envelope), padding.PKCS1v15(), hashes.SHA256())
    envelope["Signature"] = base64.b64encode(signature).decode()
    return envelope


@pytest.mark.security
def test_subscription_confirmation_is_handled_safely(harness):
    ok = harness.post_sns(_subscription("http://fake-providers:8000/ses/_confirm?token=abc"))
    assert ok.json()["status"] == "confirmed" and len(harness.remote.confirmations) == 1
    evil = harness.post_sns(_subscription("http://169.254.169.254/latest/meta-data/"))
    assert evil.json()["status"] == "ignored" and len(harness.remote.confirmations) == 1
    forged = {**_subscription("http://fake-providers:8000/ses/_confirm?token=x"), "SubscribeURL":
              "http://fake-providers:8000/ses/_confirm?token=other"}  # fmt: skip
    assert harness.post_sns(forged).status_code == 403 and len(harness.remote.confirmations) == 1


def test_unknown_messages_and_untracked_events_are_ignored(harness, tenants):
    tenant, _ = tenants
    message_id, provider_id = _sent_message(harness, tenant)
    r = harness.post_sns(harness.sns(ses_event("Delivery", "0100-unknown", "x@example.test")))
    assert r.json()["status"] == "unknown_message"
    for kind in ("Open", "Click"):
        r = harness.post_sns(harness.sns(ses_event(kind, provider_id, "ada@example.test")))
        assert r.json()["status"] == "ignored"
    soft = harness.post_sns(harness.sns(ses_event("Bounce", provider_id, "ada@example.test", bounce_type="Transient")))
    assert soft.json()["status"] == "ignored"
    row = harness.message(message_id, tenant)
    assert row["state"] == "accepted" and row["last_error"] == "soft_bounce" and row["suppression_reason"] is None


def test_event_before_acceptance_is_routed_by_our_tag_and_cannot_repoint_messages(harness, tenants):
    tenant, _ = tenants
    domain = harness.domain(tenant)
    harness.enqueued.clear()
    r = harness.internal_send(tenant, {"category": "transactional", "from_address": f"a@{domain['domain']}",
                                       "to": "t@example.test", "subject": "s", "text": "t",
                                       "source": {"type": "draft", "id": "d"}})  # fmt: skip
    message_id = r.json()["message_id"]
    harness.provider.modes = ["ambiguous_accepted"]
    harness.send_pending(tenant)
    provider_id = harness.provider.accepted[message_id]
    r = harness.post_sns(harness.sns(ses_event("Delivery", provider_id, "t@example.test", tag=message_id)))
    assert r.json()["status"] == "applied"
    row = harness.message(message_id, tenant)
    assert row["state"] == "delivered" and row["provider_message_id"] == provider_id and row["ambiguous_at"] is None
    # A second, different provider id claiming the same tag is not accepted.
    r = harness.post_sns(harness.sns(ses_event("Bounce", "0100-other", "t@example.test", tag=message_id)))
    assert r.json()["status"] == "unknown_message"
    assert harness.message(message_id, tenant)["state"] == "delivered"


def test_bounce_and_complaint_become_suppressions(harness, tenants):
    tenant, _ = tenants
    bounced_id, bounced_provider = _sent_message(harness, tenant, to="Gone@Example.test")
    complained_id, complained_provider = _sent_message(harness, tenant, to="angry@example.test")
    assert harness.post_sns(harness.sns(ses_event("Bounce", bounced_provider, "gone@example.test"))).status_code == 200
    harness.post_sns(harness.sns(ses_event("Delivery", complained_provider, "angry@example.test")))
    harness.post_sns(harness.sns(ses_event("Complaint", complained_provider, "angry@example.test")))
    assert harness.message(bounced_id, tenant)["state"] == "bounced"
    assert harness.message(complained_id, tenant)["state"] == "complained"
    complaint = [e for e in harness.events("email.complaint") if e["payload"]["message_id"] == complained_id]
    assert complaint[0]["payload"]["address_hash"] == hashlib.sha256(b"angry@example.test").hexdigest()
    assert harness.run(lambda d: sync_tenant_suppressions(d, tenant)) == 2
    posts = {p["value"].lower(): p for p in harness.remote.suppression_posts}
    assert posts["gone@example.test"]["reason"] == "bounce" and posts["angry@example.test"]["reason"] == "complaint"
    assert posts["gone@example.test"]["idempotency_key"] == f"email-suppression:{bounced_id}"
    assert harness.run(lambda d: sync_tenant_suppressions(d, tenant)) == 0  # synced once
    # Local memory blocks the address at once, even before crm answers.
    harness.remote.suppressed.clear()
    domain = harness.domain(tenant)
    r = harness.internal_send(tenant, {"category": "reply", "from_address": f"s@{domain['domain']}",
                                       "to": "gone@example.test", "subject": "s", "text": "t",
                                       "source": {"type": "draft", "id": "d"}})  # fmt: skip
    assert r.status_code == 422 and r.json()["code"] == "recipient_suppressed"


def test_bounce_rate_auto_pauses_the_campaign_and_notifies(harness, tenants):
    tenant, _ = tenants
    harness.remote.audience.clear()
    harness.remote.add_contacts(6)
    camp = harness.campaign(tenant, max_per_minute=600)
    harness.launch(tenant, camp["id"])
    harness.sql("DELETE FROM campaign_schedule_index WHERE tenant_id <> :t", {"t": str(tenant)})
    harness.enqueued.clear()
    harness.run(lambda d: scheduler_tick(d))
    harness.send_pending(tenant)
    rows = harness.sql("SELECT id, provider_message_id, to_address FROM email_messages WHERE campaign_id = :c",
                       {"c": camp["id"]}, tenant_id=tenant)  # fmt: skip
    assert len(rows) == 6 and all(r["provider_message_id"] for r in rows)
    first = rows[0]
    harness.post_sns(harness.sns(ses_event("Bounce", first["provider_message_id"], first["to_address"])))
    state = harness.sql("SELECT state, pause_reason FROM email_campaigns WHERE id = :c", {"c": camp["id"]},
                        tenant_id=tenant)[0]  # fmt: skip
    assert state == {"state": "paused", "pause_reason": "bounce_rate"}  # 1/6 > 5 % with min sample 4
    notes = [e for e in harness.events("notification.requested") if e["payload"]["resource_id"] == camp["id"]]
    assert notes and notes[0]["payload"]["permission"] == "emails:campaigns:manage"
    audits = [e for e in harness.events("audit.event") if e["payload"]["action"] == "email_campaign.auto_paused"
              and e["payload"]["resource_id"] == camp["id"]]  # fmt: skip
    assert len(audits) == 1
    report = harness.client.get(f"/api/v1/email/campaigns/{camp['id']}/report", headers=harness.user(tenant)).json()
    assert report["counts"]["bounced"] == 1 and report["rates"]["bounce_rate"] == round(1 / 6, 4)
    assert "open_rate" not in report["rates"]


# ---------------------------------------------------------------------------------------------- unsubscribe
def _campaign_token(harness, tenant):
    harness.remote.audience.clear()
    [contact] = harness.remote.add_contacts(1)
    camp = harness.campaign(tenant)
    harness.launch(tenant, camp["id"])
    harness.sql("DELETE FROM campaign_schedule_index WHERE tenant_id <> :t", {"t": str(tenant)})
    harness.enqueued.clear()
    harness.run(lambda d: scheduler_tick(d))
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "accepted"
    sent = harness.provider.sent[-1]
    url = sent.headers["List-Unsubscribe"].strip("<>")
    token = url.rsplit("/u/", 1)[1]
    message = harness.sql("SELECT id FROM email_messages WHERE campaign_id = :c", {"c": camp["id"]},
                          tenant_id=tenant)[0]  # fmt: skip
    return token, str(message["id"]), contact, sent.to_address, camp


@pytest.mark.security
def test_one_click_unsubscribe(harness, tenants):
    tenant, _ = tenants
    token, message_id, contact, address, _camp = _campaign_token(harness, tenant)
    page = harness.client.get(f"/u/{token}")
    assert page.status_code == 200 and 'method="post"' in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert harness.message(message_id, tenant)["state"] == "accepted"  # GET never unsubscribes
    r = harness.client.post(f"/u/{token}", content=b"List-Unsubscribe=One-Click",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert r.status_code == 200 and "unsubscribed" in r.text
    row = harness.message(message_id, tenant)
    assert row["state"] == "unsubscribed" and row["suppression_reason"] == "opt_out"
    events = [e for e in harness.events("email.unsubscribed") if e["payload"]["message_id"] == message_id]
    assert len(events) == 1 and events[0]["payload"]["contact_id"] == str(contact)
    assert events[0]["payload"]["address_hash"] == hashlib.sha256(address.strip().lower().encode()).hexdigest()
    assert [p["reason"] for p in harness.remote.suppression_posts if p["value"] == address] == ["opt_out"]
    assert row["suppression_synced_at"] is not None
    again = harness.client.post(f"/u/{token}", content=b"List-Unsubscribe=One-Click")
    assert again.status_code == 200
    assert len([e for e in harness.events("email.unsubscribed") if e["payload"]["message_id"] == message_id]) == 1
    tokens = harness.sql("SELECT token_hash FROM unsubscribe_tokens WHERE message_id = :m", {"m": message_id},
                         tenant_id=tenant)  # fmt: skip
    assert tokens[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest() and token not in str(tokens)


@pytest.mark.security
def test_invalid_unsubscribe_tokens_are_indistinguishable(harness):
    a = harness.client.post("/u/" + "a" * 43)
    b = harness.client.post("/u/" + "b" * 43)
    c = harness.client.get("/u/short")
    assert a.status_code == b.status_code == c.status_code == 404 and a.text == b.text


def test_unsubscribe_is_honoured_at_send_time_for_marketing_only(harness, tenants):
    tenant, _ = tenants
    token, _message_id, _contact, address, _camp = _campaign_token(harness, tenant)
    harness.client.post(f"/u/{token}", content=b"List-Unsubscribe=One-Click")
    harness.remote.suppressed.clear()  # prove the local memory alone blocks the next campaign
    harness.remote.audience.clear()
    cid = uuid.uuid4()
    harness.remote.audience.append(cid)
    harness.remote.contacts[str(cid)] = {"contact_id": str(cid), "allowed": True, "reasons": [], "email": address,
                                         "first_name": "A", "last_name": "B"}  # fmt: skip
    camp = harness.campaign(tenant)
    harness.launch(tenant, camp["id"])
    harness.sql("DELETE FROM campaign_schedule_index WHERE tenant_id <> :t", {"t": str(tenant)})
    harness.enqueued.clear()
    harness.run(lambda d: scheduler_tick(d))
    sent_before = len(harness.provider.sent)
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "failed" and outcome.detail == "suppressed_opt_out"
    assert len(harness.provider.sent) == sent_before
    # A reply to the same person is still allowed (separate transactional stream).
    domain = harness.domain(tenant)
    r = harness.internal_send(tenant, {"category": "reply", "from_address": f"s@{domain['domain']}", "to": address,
                                       "subject": "Re: question", "text": "Answer",
                                       "source": {"type": "draft", "id": "d"}})  # fmt: skip
    assert r.status_code == 202
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "accepted"
