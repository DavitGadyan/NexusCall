"""Public API against a real database: domains, senders, templates, campaigns, permissions, isolation and
idempotency."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from email_service.tables import template_versions

from .conftest import MANAGE, table_rows

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------------------- domains / senders
def test_adding_a_domain_shows_dkim_records_and_verification_gates_status(harness, tenants):
    tenant, _ = tenants
    headers = harness.user(tenant)
    r = harness.client.post("/api/v1/email/domains", headers=headers, json={"domain": "News.Acme-Demo.TEST"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["domain"] == "news.acme-demo.test" and body["status"] == "pending"
    kinds = {rec["purpose"] for rec in body["records"]}
    assert kinds == {"dkim", "spf", "dmarc"}
    assert sum(rec["type"] == "CNAME" for rec in body["records"]) == 3
    verified = harness.client.post(f"/api/v1/email/domains/{body['id']}/verify", headers=headers).json()
    assert verified["status"] == "verified" and verified["dkim_status"] == "pass" and verified["verified_at"]
    bad = harness.domain(tenant, "unverified-shop.test")
    assert bad["status"] == "failed" and bad["dkim_status"] == "fail"
    dup = harness.client.post("/api/v1/email/domains", headers=headers, json={"domain": "news.acme-demo.test"})
    assert dup.status_code == 409
    assert (
        harness.client.post("/api/v1/email/domains", headers=headers, json={"domain": "no spaces"}).status_code == 422
    )


def test_sender_validation_and_optimistic_locking(harness, tenants):
    tenant, _ = tenants
    domain = harness.domain(tenant)
    headers = harness.user(tenant)
    for bad in ({"from_name": "Evil\r\nBcc: x@y.test", "local_part": "a"}, {"from_name": "A", "local_part": "a b"},
                {"from_name": "A", "local_part": "a", "reply_to": "not-an-address"}):  # fmt: skip
        r = harness.client.post("/api/v1/email/senders", headers=headers, json={"domain_id": domain["id"], **bad})
        assert r.status_code == 422, bad
    sender = harness.sender(tenant, domain)
    assert sender["from_address"] == f"news@{domain['domain']}" and sender["domain_status"] == "verified"
    r = harness.client.patch(f"/api/v1/email/senders/{sender['id']}", headers=headers,
                             json={"expected_version": 1, "status": "disabled"})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    stale = harness.client.patch(f"/api/v1/email/senders/{sender['id']}", headers=headers,
                                 json={"expected_version": 1, "from_name": "X"})  # fmt: skip
    assert stale.status_code == 409
    # A domain with senders cannot be deleted.
    assert harness.client.delete(f"/api/v1/email/domains/{domain['id']}", headers=headers).status_code == 409


# ----------------------------------------------------------------------------------------------- templates
def test_unsafe_templates_are_rejected_with_reasons(harness, tenants):
    tenant, _ = tenants
    r = harness.client.post(
        "/api/v1/email/templates",
        headers=harness.user(tenant),
        json={"name": "bad", "subject": "Hi", "html": '<a href="javascript:alert(1)">x</a><script>x</script>'},
    )
    assert r.status_code == 422
    problems = r.json()["problems"]
    assert any("http or https" in p for p in problems) and any("<script>" in p for p in problems)


def test_template_versions_are_append_only_and_preview_escapes(harness, tenants, sdb):
    tenant, _ = tenants
    headers = harness.user(tenant)
    t = harness.template(tenant)
    assert t["current"]["version"] == 1
    r = harness.client.put(f"/api/v1/email/templates/{t['id']}", headers=headers,
                           json={"expected_version": 1, "subject": "v2 {{first_name}}", "html": "<p>v2</p>"})  # fmt: skip
    assert r.status_code == 200 and r.json()["current_version"] == 2
    stale = harness.client.put(f"/api/v1/email/templates/{t['id']}", headers=headers,
                               json={"expected_version": 1, "subject": "x", "html": "<p>x</p>"})  # fmt: skip
    assert stale.status_code == 409
    versions = harness.client.get(f"/api/v1/email/templates/{t['id']}/versions", headers=headers).json()["items"]
    assert [v["version"] for v in versions] == [2, 1]
    preview = harness.client.post(f"/api/v1/email/templates/{t['id']}/preview", headers=headers,
                                  json={"version": 1, "variables": {"first_name": "<b>Eve</b>"}}).json()  # fmt: skip
    assert "&lt;b&gt;Eve&lt;/b&gt;" in preview["html"] and "Unsubscribe" in preview["html"]
    # History cannot be rewritten, even with SQL as the application role.
    with pytest.raises(Exception, match="append-only|permission denied|row-level security"):
        harness.sql("UPDATE template_versions SET subject = 'hacked'", tenant_id=tenant)
    assert all(v["subject"] != "hacked" for v in table_rows(harness, template_versions, tenant))


def test_test_send_goes_through_the_pipeline_and_is_bounded(harness, tenants):
    tenant, _ = tenants
    sender = harness.sender(tenant)
    t = harness.template(tenant, category="transactional", html="<p>Receipt for {{first_name}}</p>")
    headers = harness.user(tenant)
    body = {"sender_id": sender["id"], "to": "qa@example.test", "variables": {"first_name": "Ada"}}
    r = harness.client.post(f"/api/v1/email/templates/{t['id']}/test-send", headers={**headers, **harness.ik("t1")},
                            json=body)  # fmt: skip
    assert r.status_code == 202, r.text
    replay = harness.client.post(f"/api/v1/email/templates/{t['id']}/test-send",
                                 headers={**headers, **harness.ik("t1")}, json=body)  # fmt: skip
    assert replay.json() == r.json()
    assert harness.client.post(f"/api/v1/email/templates/{t['id']}/test-send", headers=headers,
                               json=body).status_code == 422  # Idempotency-Key required  # fmt: skip
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "accepted"
    sent = harness.provider.sent[-1]
    assert sent.subject.startswith("[TEST]") and "List-Unsubscribe" not in sent.headers
    harness.settings.test_sends_per_hour = 1
    r = harness.client.post(f"/api/v1/email/templates/{t['id']}/test-send", headers={**headers, **harness.ik()},
                            json=body)  # fmt: skip
    assert r.status_code == 429


# ----------------------------------------------------------------------------------------------- campaigns
def test_launch_is_idempotent_audited_and_validated(harness, tenants):
    tenant, _ = tenants
    camp = harness.campaign(tenant)
    first = harness.launch(tenant, camp["id"], key="launch-1")
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "scheduled" and first.json()["template_version"] == 1
    replay = harness.launch(tenant, camp["id"], key="launch-1")
    assert replay.status_code == 202 and replay.json() == first.json()
    other = harness.launch(tenant, camp["id"], key="launch-2")
    assert other.status_code == 409  # already launched
    mismatch = harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/schedule",
                                   headers={**harness.user(tenant), **harness.ik("launch-1")},
                                   json={"scheduled_at": "2030-01-01T00:00:00Z"})  # fmt: skip
    assert mismatch.status_code == 422  # same key, different payload
    audits = [e for e in harness.events("audit.event") if e["payload"]["action"] == "email_campaign.launched"]
    assert len([a for a in audits if a["payload"]["resource_id"] == camp["id"]]) == 1
    index = harness.sql("SELECT tenant_id FROM campaign_schedule_index WHERE campaign_id = :c", {"c": camp["id"]})
    assert [str(r["tenant_id"]) for r in index] == [str(tenant)]


def test_concurrent_duplicate_launches_produce_one_launch(harness, tenants):
    tenant, _ = tenants
    camp = harness.campaign(tenant)
    keys = [str(uuid.uuid4()) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda k: harness.launch(tenant, camp["id"], key=k).status_code, keys))
    assert results.count(202) == 1 and set(results) <= {202, 409}


def test_launch_requires_verified_domain_segment_and_enabled_email(harness, tenants):
    tenant, _ = tenants
    unverified = harness.sender(tenant, harness.domain(tenant, "unverified-x.test"))
    camp = harness.campaign(tenant, sender=unverified)
    r = harness.launch(tenant, camp["id"])
    assert r.status_code == 409 and r.json()["code_detail"] == "sender_domain_not_verified"
    ok = harness.campaign(tenant)
    harness.remote.segment_missing = True
    assert harness.launch(tenant, ok["id"]).status_code == 422
    harness.remote.segment_missing = False
    harness.remote.comm["effective_emails_enabled"] = False
    r = harness.launch(tenant, ok["id"])
    assert r.status_code == 409 and r.json()["code_detail"] == "emails_disabled"
    harness.remote.comm["effective_emails_enabled"] = True
    assert harness.launch(tenant, ok["id"]).status_code == 202


def test_transactional_templates_cannot_drive_campaigns(harness, tenants):
    tenant, _ = tenants
    sender = harness.sender(tenant)
    t = harness.template(tenant, category="transactional")
    r = harness.client.post("/api/v1/email/campaigns", headers=harness.user(tenant),
                            json={"name": "x", "sender_id": sender["id"], "template_id": t["id"],
                                  "segment_id": str(uuid.uuid4())})  # fmt: skip
    assert r.status_code == 422


def test_pause_resume_cancel_lifecycle(harness, tenants):
    tenant, _ = tenants
    camp = harness.campaign(tenant)
    headers = harness.user(tenant)
    assert (
        harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/pause", headers=headers, json={}).status_code == 409
    )
    harness.launch(tenant, camp["id"])
    paused = harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/pause", headers=headers, json={})
    assert paused.json()["state"] == "paused" and paused.json()["pause_reason"] == "manual"
    resumed = harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/resume", headers=headers, json={})
    assert resumed.json()["state"] == "scheduled"  # never started yet
    canceled = harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/cancel", headers=headers, json={})
    assert canceled.json()["state"] == "canceled"
    again = harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/resume", headers=headers, json={})
    assert again.status_code == 409
    edit = harness.client.patch(f"/api/v1/email/campaigns/{camp['id']}", headers=headers,
                                json={"expected_version": canceled.json()["version"], "name": "x"})  # fmt: skip
    assert edit.status_code == 409


# ------------------------------------------------------------------------------------- permissions / tenancy
@pytest.mark.security
def test_permission_boundaries(harness, tenants):
    tenant, _ = tenants
    camp = harness.campaign(tenant)
    reader = harness.user(tenant, {"campaigns:read"})
    nobody = harness.user(tenant, {"leads:read"})
    assert harness.client.get(f"/api/v1/email/campaigns/{camp['id']}", headers=reader).status_code == 200
    assert harness.client.get(f"/api/v1/email/campaigns/{camp['id']}/report", headers=reader).status_code == 200
    assert harness.client.get("/api/v1/email/messages", headers=reader).status_code == 200
    for method, path, body in [
        ("post", f"/api/v1/email/campaigns/{camp['id']}/launch", None),
        ("post", f"/api/v1/email/campaigns/{camp['id']}/pause", {}),
        ("post", "/api/v1/email/campaigns", {}),
        ("get", "/api/v1/email/domains", None),
        ("post", "/api/v1/email/domains", {"domain": "x.test"}),
        ("get", "/api/v1/email/senders", None),
        ("get", "/api/v1/email/templates", None),
    ]:
        r = getattr(harness.client, method)(
            path, headers={**reader, **harness.ik()}, **({"json": body} if body is not None else {})
        )
        assert r.status_code == 403, (method, path)
    for path in ("/api/v1/email/campaigns", f"/api/v1/email/campaigns/{camp['id']}", "/api/v1/email/usage"):
        assert harness.client.get(path, headers=nobody).status_code == 403
    assert harness.client.get("/api/v1/email/campaigns").status_code == 401


@pytest.mark.security
def test_cross_tenant_resources_are_invisible(harness, tenants):
    tenant_a, tenant_b = tenants
    camp = harness.campaign(tenant_a)
    domain = harness.domain(tenant_a)
    t = harness.template(tenant_a)
    sender_a = harness.sender(tenant_a)
    b = harness.user(tenant_b, {MANAGE, "campaigns:read"})
    for path in (f"/api/v1/email/campaigns/{camp['id']}", f"/api/v1/email/campaigns/{camp['id']}/report",
                 f"/api/v1/email/domains/{domain['id']}", f"/api/v1/email/templates/{t['id']}",
                 f"/api/v1/email/senders/{sender_a['id']}"):  # fmt: skip
        assert harness.client.get(path, headers=b).status_code == 404, path
    assert harness.client.patch(f"/api/v1/email/campaigns/{camp['id']}", headers=b,
                                json={"expected_version": 1, "name": "x"}).status_code == 404  # fmt: skip
    assert harness.client.delete(f"/api/v1/email/domains/{domain['id']}", headers=b).status_code == 404
    assert harness.client.post(f"/api/v1/email/domains/{domain['id']}/verify", headers=b).status_code == 404
    assert harness.launch(tenant_b, camp["id"]).status_code == 404
    assert harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/cancel", headers=b, json={}).status_code == 404
    # Tenant B cannot build a campaign on tenant A's sender or template (composite FK + RLS).
    own = harness.template(tenant_b)
    r = harness.client.post("/api/v1/email/campaigns", headers=b,
                            json={"name": "x", "sender_id": sender_a["id"], "template_id": own["id"],
                                  "segment_id": str(uuid.uuid4())})  # fmt: skip
    assert r.status_code == 404
    assert harness.client.get("/api/v1/email/campaigns", headers=b).json()["items"] == []
    # A user of tenant A selecting tenant B is refused outright.
    a_user_sub = str(uuid.uuid4())
    harness.authz.grant(a_user_sub, tenant_a, {MANAGE})
    from nexus_core.testing import auth

    token = harness.tokens.user(a_user_sub, "a@example.test")
    assert harness.client.get("/api/v1/email/campaigns", headers=auth(token, tenant_b)).status_code == 403


@pytest.mark.security
def test_rls_hides_rows_even_with_raw_sql(harness, tenants):
    tenant_a, tenant_b = tenants
    harness.campaign(tenant_a)
    seen = harness.sql("SELECT DISTINCT tenant_id FROM email_campaigns", tenant_id=tenant_b)
    assert seen == []
    assert harness.sql("SELECT count(*) AS n FROM email_campaigns")[0]["n"] == 0  # no tenant context: nothing
