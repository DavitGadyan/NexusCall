"""`/internal/v1/send` and `/internal/v1/messages/lookup` exactly as BUILD_CONVENTIONS §5."""

from __future__ import annotations

import uuid

import pytest

from .conftest import INBOX_CLIENT

pytestmark = [pytest.mark.integration]


def _body(domain: str, **over):
    return {
        "category": "reply",
        "from_address": f"support@{domain}",
        "to": "customer@example.test",
        "subject": "Re: your question",
        "text": "Here is the answer.",
        "html": '<p onclick="x()">Here is <a href="javascript:alert(1)">the</a> <a href="https://www.example.com/">answer</a></p><script>x</script>',
        "headers": {
            "In-Reply-To": "<orig-1@mail.example.test>",
            "References": "<root@mail.example.test> <orig-1@mail.example.test>",
        },
        "source": {"type": "draft", "id": "draft-1", "version": 2},
        **over,
    }


def test_send_queues_a_sanitized_reply_with_threading_headers(harness, tenants):
    tenant, _ = tenants
    domain = harness.domain(tenant)["domain"]
    harness.enqueued.clear()
    r = harness.internal_send(tenant, _body(domain), key="draft:1:v2")
    assert r.status_code == 202 and r.json()["state"] == "queued"
    message_id = r.json()["message_id"]
    row = harness.message(message_id, tenant)
    assert row["category"] == "reply" and row["source_type"] == "draft" and row["source_version"] == 2
    assert (
        "onclick" not in row["body_html"]
        and "javascript" not in row["body_html"]
        and "<script>" not in row["body_html"]
    )
    assert [j[0] for j in harness.enqueued] == ["emails.send_message"]
    [outcome] = harness.send_pending(tenant)
    sent = harness.provider.sent[-1]
    assert outcome.status == "accepted" and sent.category == "reply"
    assert sent.headers["In-Reply-To"] == "<orig-1@mail.example.test>"
    assert sent.headers["References"].endswith("<orig-1@mail.example.test>")
    assert "List-Unsubscribe" not in sent.headers and sent.headers["Message-ID"] == row["rfc_message_id"]
    # Lookup by RFC Message-ID, with or without angle brackets.
    headers = harness.service(INBOX_CLIENT, tenant)
    found = harness.client.get("/internal/v1/messages/lookup", headers=headers,
                               params={"rfc_message_id": row["rfc_message_id"].strip("<>")})  # fmt: skip
    assert found.status_code == 200 and found.json() == {"message_id": message_id, "campaign_id": None,
                                                         "contact_id": None}  # fmt: skip


def test_send_idempotency(harness, tenants):
    tenant, _ = tenants
    domain = harness.domain(tenant)["domain"]
    harness.enqueued.clear()
    first = harness.internal_send(tenant, _body(domain), key="k-1")
    replay = harness.internal_send(tenant, _body(domain), key="k-1")
    assert replay.status_code == 202 and replay.json() == first.json()
    assert len(harness.enqueued) == 1
    different = harness.internal_send(tenant, _body(domain, subject="Changed"), key="k-1")
    assert different.status_code == 422
    no_key = harness.client.post("/internal/v1/send", json=_body(domain), headers=harness.service(INBOX_CLIENT, tenant))
    assert no_key.status_code == 422
    count = harness.sql("SELECT count(*) AS n FROM email_messages WHERE source_id = 'draft-1'", tenant_id=tenant)
    assert count[0]["n"] == 1


def test_send_rejections(harness, tenants):
    tenant, _ = tenants
    verified = harness.domain(tenant)["domain"]
    unverified = harness.domain(tenant, "unverified-mail.test")["domain"]
    r = harness.internal_send(tenant, _body(unverified), key="u-1")
    assert r.status_code == 422 and r.json()["code"] == "sender_domain_not_verified"
    assert harness.internal_send(tenant, _body(unverified), key="u-1").json()["code"] == "sender_domain_not_verified"
    r = harness.internal_send(tenant, _body("not-registered.test"))
    assert r.status_code == 422 and r.json()["code"] == "sender_domain_not_verified"
    harness.remote.suppressed["customer@example.test"] = ["bounce"]
    r = harness.internal_send(tenant, _body(verified))
    assert r.status_code == 422 and r.json()["code"] == "recipient_suppressed"
    harness.remote.suppressed["customer@example.test"] = ["opt_out"]  # marketing opt-out does not block replies
    assert harness.internal_send(tenant, _body(verified)).status_code == 202
    harness.remote.suppressed.clear()
    harness.remote.comm["effective_emails_enabled"] = False
    r = harness.internal_send(tenant, _body(verified), key="ks-1")
    assert r.status_code == 409 and r.json()["code"] == "emails_disabled"
    harness.remote.comm["effective_emails_enabled"] = True
    assert harness.internal_send(tenant, _body(verified), key="ks-1").status_code == 202  # retryable outcome
    harness.remote.crm_down = True
    r = harness.internal_send(tenant, _body(verified), key="down-1")
    assert r.status_code == 503
    harness.remote.crm_down = False
    assert harness.internal_send(tenant, _body(verified), key="down-1").status_code == 202


@pytest.mark.parametrize(
    "over",
    [
        {"subject": "Hi\r\nBcc: victim@evil.test"},
        {"headers": {"In-Reply-To": "<a@b.test>\r\nBcc: victim@evil.test"}},
        {"headers": {"References": "not a message id"}},
        {"headers": {"Bcc": "victim@evil.test"}},
        {"to": "victim@evil.test\r\nBcc: x@y.test"},
        {"to": "not-an-address"},
        {"from_address": "a@b.test, c@d.test"},
        {"category": "campaign"},
        {"source": {"type": "draft", "id": "x y"}},
        {"unexpected": "field"},
    ],
)
@pytest.mark.security
def test_send_validation_blocks_header_injection(harness, tenants, over):
    tenant, _ = tenants
    domain = harness.domain(tenant)["domain"]
    assert harness.internal_send(tenant, _body(domain, **over)).status_code == 422


@pytest.mark.security
def test_internal_endpoints_accept_only_the_allowlisted_service(harness, tenants):
    tenant, other = tenants
    domain = harness.domain(tenant)["domain"]
    user = harness.user(tenant)
    assert (
        harness.client.post("/internal/v1/send", json=_body(domain), headers={**user, **harness.ik()}).status_code
        == 403
    )
    rogue = harness.service("svc-crm-service", tenant)
    assert (
        harness.client.post("/internal/v1/send", json=_body(domain), headers={**rogue, **harness.ik()}).status_code
        == 403
    )
    no_tenant = harness.service(INBOX_CLIENT, None)
    assert harness.client.post("/internal/v1/send", json=_body(domain),
                               headers={**no_tenant, **harness.ik()}).status_code == 403  # fmt: skip
    assert harness.client.get("/internal/v1/messages/lookup", headers=rogue,
                              params={"rfc_message_id": "<x@y>"}).status_code == 403  # fmt: skip
    # Tenant B (asserted by the caller) cannot use tenant A's verified domain nor find A's messages.
    r = harness.internal_send(other, _body(domain))
    assert r.status_code == 422 and r.json()["code"] == "sender_domain_not_verified"
    harness.enqueued.clear()
    sent = harness.internal_send(tenant, _body(domain)).json()["message_id"]
    rfc = harness.message(sent, tenant)["rfc_message_id"]
    missing = harness.client.get("/internal/v1/messages/lookup", headers=harness.service(INBOX_CLIENT, other),
                                 params={"rfc_message_id": rfc})  # fmt: skip
    assert missing.status_code == 404
    unknown = harness.client.get("/internal/v1/messages/lookup", headers=harness.service(INBOX_CLIENT, tenant),
                                 params={"rfc_message_id": f"<{uuid.uuid4().hex}@x.test>"})  # fmt: skip
    assert unknown.status_code == 404
