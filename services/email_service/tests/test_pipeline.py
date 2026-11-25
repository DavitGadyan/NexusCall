"""Campaign scheduler, the send task and reconciliation, against a real database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from email_service.jobs import run_campaign, scheduler_tick, sweep_tenant
from email_service.sending import send_message
from nexus_core.celery_app import JobContext

pytestmark = [pytest.mark.integration]


def _ctx(tenant: uuid.UUID) -> JobContext:
    return JobContext(tenant_id=tenant, correlation_id=uuid.uuid4())


def _launched(harness, tenant, n=3, **campaign_extra):
    harness.remote.add_contacts(n)
    camp = harness.campaign(tenant, **campaign_extra)
    assert harness.launch(tenant, camp["id"]).status_code == 202
    return camp


def _tick(harness, tenant=None):
    """One scheduler tick. Campaigns left behind by other tests (shared session database) are unindexed."""
    tenant = tenant or harness.current_tenant
    harness.sql("DELETE FROM campaign_schedule_index WHERE tenant_id <> :t", {"t": str(tenant)})
    return harness.run(lambda d: scheduler_tick(d))


@pytest.fixture(autouse=True)
def _remember_tenant(harness, tenants):
    harness.current_tenant = tenants[0]


def _states(harness, tenant, campaign_id):
    rows = harness.sql("SELECT state, count(*) AS n FROM email_messages WHERE campaign_id = :c GROUP BY state",
                       {"c": campaign_id}, tenant_id=tenant)  # fmt: skip
    return {r["state"]: r["n"] for r in rows}


def _reset_audience(harness):
    harness.remote.audience.clear()


def test_campaign_end_to_end_snapshot_batches_send_and_complete(harness, tenants):
    tenant, _ = tenants
    _reset_audience(harness)
    camp = _launched(harness, tenant, n=5, max_per_minute=24)  # 24/min * 5 s tick = 2 per tick
    assert _tick(harness) == 2
    state = harness.sql("SELECT state, snapshot_done FROM email_campaigns WHERE id = :c", {"c": camp["id"]},
                        tenant_id=tenant)[0]  # fmt: skip
    assert state == {"state": "running", "snapshot_done": True}
    assert _states(harness, tenant, camp["id"]) == {"pending": 3, "queued": 2}
    # Bounded in-flight work: with 2 queued and an allowance of 2, at most 2 more are queued.
    assert _tick(harness) == 2
    assert _states(harness, tenant, camp["id"]) == {"pending": 1, "queued": 4}
    outcomes = harness.send_pending(tenant)
    assert [o.status for o in outcomes] == ["accepted"] * 4
    _tick(harness)
    harness.send_pending(tenant)
    assert _states(harness, tenant, camp["id"]) == {"accepted": 5}
    _tick(harness)
    final = harness.sql("SELECT state FROM email_campaigns WHERE id = :c", {"c": camp["id"]}, tenant_id=tenant)
    assert final[0]["state"] == "completed"
    # Every marketing message carries RFC 8058 headers, a visible link and the escaped first name.
    msg = harness.provider.sent[0]
    assert msg.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg.headers["List-Unsubscribe"].startswith("<https://app.test/u/")
    assert "Unsubscribe" in (msg.html or "") and "/u/" in msg.text and "Hi P" in (msg.html or "")
    accepted = [e for e in harness.events("email.message.state_changed") if e["payload"]["state"] == "accepted"]
    assert len([e for e in accepted if e["payload"]["campaign_id"] == camp["id"]]) == 5


def test_snapshot_is_unique_per_campaign_and_contact(harness, tenants):
    tenant, _ = tenants
    _reset_audience(harness)
    camp = _launched(harness, tenant, n=3)
    _tick(harness)
    # Re-reading the same crm page (e.g. a crashed tick) cannot create duplicates.
    harness.sql("UPDATE email_campaigns SET snapshot_done = false, snapshot_cursor = NULL WHERE id = :c",
                {"c": camp["id"]}, tenant_id=tenant)  # fmt: skip
    _tick(harness)
    total = harness.sql("SELECT count(*) AS n FROM email_messages WHERE campaign_id = :c", {"c": camp["id"]},
                        tenant_id=tenant)[0]["n"]  # fmt: skip
    assert total == 3


def test_contacts_not_allowed_by_crm_are_skipped(harness, tenants):
    tenant, _ = tenants
    _reset_audience(harness)
    ids = harness.remote.add_contacts(3)
    harness.remote.contacts[str(ids[0])] |= {"allowed": False, "reasons": ["suppressed"]}
    del harness.remote.contacts[str(ids[1])]  # crm omits unknown ids
    camp = harness.campaign(tenant)
    harness.launch(tenant, camp["id"])
    _tick(harness)
    rows = harness.sql("SELECT contact_id, state, last_error FROM email_messages WHERE campaign_id = :c",
                       {"c": camp["id"]}, tenant_id=tenant)  # fmt: skip
    by = {str(r["contact_id"]): (r["state"], r["last_error"]) for r in rows}
    assert by[str(ids[0])] == ("failed", "skipped_suppressed")
    assert by[str(ids[1])] == ("failed", "skipped_contact_not_found")
    assert by[str(ids[2])][0] == "queued"


def test_kill_switch_window_and_schedule_hold_the_campaign(harness, tenants):
    tenant, _ = tenants
    _reset_audience(harness)
    camp = _launched(harness, tenant, n=2)
    harness.remote.comm["effective_emails_enabled"] = False
    assert _tick(harness) == 0
    assert _states(harness, tenant, camp["id"]) == {}
    harness.remote.comm["effective_emails_enabled"] = True
    harness.remote.comm["email_window"] = {"start": "00:00", "end": "00:01", "days": ["mon"]}
    assert (
        harness.run(
            lambda d: run_campaign(d, tenant, uuid.UUID(camp["id"]), now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
        )
        == 0
    )  # Tuesday
    future = harness.campaign(tenant)
    r = harness.client.post(f"/api/v1/email/campaigns/{future['id']}/schedule",
                            headers={**harness.user(tenant), **harness.ik()},
                            json={"scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()})  # fmt: skip
    assert r.status_code == 202
    harness.remote.comm["email_window"] = {"start": "00:00", "end": "23:59",
                                           "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}  # fmt: skip
    _tick(harness)
    st = harness.sql("SELECT state FROM email_campaigns WHERE id = :c", {"c": future["id"]}, tenant_id=tenant)
    assert st[0]["state"] == "scheduled"


def test_send_time_kill_switch_defers_and_paused_campaign_does_not_send(harness, tenants):
    tenant, _ = tenants
    _reset_audience(harness)
    camp = _launched(harness, tenant, n=2)
    _tick(harness)
    harness.remote.comm["effective_emails_enabled"] = False
    outcomes = harness.send_pending(tenant)
    assert {o.detail for o in outcomes} == {"emails_disabled"} and harness.provider.sent == []
    assert _states(harness, tenant, camp["id"]) == {"queued": 2}
    harness.remote.comm["effective_emails_enabled"] = True
    harness.client.post(f"/api/v1/email/campaigns/{camp['id']}/pause", headers=harness.user(tenant), json={})
    harness.sql("UPDATE email_messages SET next_attempt_at = NULL WHERE campaign_id = :c", {"c": camp["id"]},
                tenant_id=tenant)  # fmt: skip
    outcomes = harness.send_pending(tenant)
    assert all(o.status == "noop" for o in outcomes) and harness.provider.sent == []


def _internal(harness, tenant, to="buyer@example.test", **over):
    domain = harness.domain(tenant)
    body = {"category": "transactional", "from_address": f"support@{domain['domain']}", "to": to,
            "subject": "Your receipt", "text": "Thanks", "source": {"type": "draft", "id": "d1", "version": 1},
            **over}  # fmt: skip
    r = harness.internal_send(tenant, body)
    assert r.status_code == 202, r.text
    return r.json()["message_id"]


def test_suppression_is_honoured_at_send_time(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    message_id = _internal(harness, tenant, to="late@example.test")
    harness.remote.suppressed["late@example.test"] = ["complaint"]  # suppressed after it was queued
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "failed" and outcome.detail == "suppressed"
    assert harness.provider.sent == [] and harness.message(message_id, tenant)["state"] == "failed"
    # Opt-out alone does not stop transactional mail (separate streams)...
    second = _internal(harness, tenant, to="optout@example.test")
    harness.remote.suppressed["optout@example.test"] = ["opt_out"]
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "accepted" and harness.message(second, tenant)["state"] == "accepted"


def test_daily_quota_and_rate_limits_defer(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    harness.remote.comm["email_daily_quota"] = 1
    first = _internal(harness, tenant)
    second = _internal(harness, tenant, to="b@example.test")
    outcomes = harness.send_pending(tenant)
    assert [o.status for o in outcomes] == ["accepted", "deferred"] and outcomes[1].detail == "daily_quota"
    row = harness.message(second, tenant)
    assert row["state"] == "queued" and row["next_attempt_at"] > datetime.now(UTC) + timedelta(minutes=1)
    assert harness.message(first, tenant)["state"] == "accepted"
    usage = harness.client.get("/api/v1/email/usage", headers=harness.user(tenant, {"campaigns:read"})).json()
    assert usage["days"][0]["sent"] == 1 and usage["daily_quota"] == 1
    harness.remote.comm["email_daily_quota"] = 5000
    third = _internal(harness, tenant, to="c@example.test")
    harness.limiter.waits["email:rate:domain:"] = 0.4
    [outcome] = [o for o in harness.send_pending(tenant)]
    assert outcome.status == "deferred" and outcome.detail == "rate_domain" and outcome.delay == 0.4
    republished = [j for j in harness.enqueued if j[3]["message_id"] == third]
    assert republished and 0.4 <= republished[0][3]["countdown"] <= 1.0  # re-published with a countdown
    harness.limiter.waits.clear()
    harness.limiter.waits["email:rate:provider:ses"] = 2.0
    harness.sql("UPDATE email_messages SET next_attempt_at = NULL WHERE id = :i", {"i": third}, tenant_id=tenant)
    [outcome] = harness.send_pending(tenant)
    assert outcome.detail == "rate_provider"
    harness.limiter.waits.clear()
    keys = set(harness.limiter.keys)
    assert any(k.endswith(":transactional") for k in keys)


def test_duplicate_task_delivery_sends_once(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    message_id = _internal(harness, tenant)
    ctx = _ctx(tenant)
    first = harness.run(lambda d: send_message(d, ctx, uuid.UUID(message_id)))
    again = harness.run(lambda d: send_message(d, ctx, uuid.UUID(message_id)))
    assert first.status == "accepted" and again.status == "noop"
    assert len([m for m in harness.provider.sent if m.idempotency_key == message_id]) == 1


def test_ambiguous_error_is_reconciled_without_a_duplicate(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    message_id = _internal(harness, tenant)
    harness.provider.modes = ["ambiguous_accepted"]  # the provider accepted it but the answer was lost
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "ambiguous"
    row = harness.message(message_id, tenant)
    assert row["state"] == "sending" and row["ambiguous_at"] is not None
    # A redelivered task does not resend.
    assert harness.run(lambda d: send_message(d, _ctx(tenant), uuid.UUID(message_id))).status == "noop"
    later = datetime.now(UTC) + timedelta(seconds=harness.settings.stuck_sending_seconds + 5)
    result = harness.run(lambda d: sweep_tenant(d, tenant, now=later))
    assert result["reconciled"] == 1
    row = harness.message(message_id, tenant)
    assert row["state"] == "accepted" and row["provider_message_id"] == harness.provider.accepted[message_id]
    assert len([m for m in harness.provider.sent if m.idempotency_key == message_id]) == 1


def test_ambiguous_error_not_accepted_is_resent_with_the_same_key(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    message_id = _internal(harness, tenant)
    harness.provider.modes = ["ambiguous"]
    harness.send_pending(tenant)
    later = datetime.now(UTC) + timedelta(seconds=harness.settings.stuck_sending_seconds + 5)
    assert harness.run(lambda d: sweep_tenant(d, tenant, now=later))["requeued"] == 1
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "accepted"
    keys = [m.idempotency_key for m in harness.provider.sent if m.idempotency_key == message_id]
    assert len(keys) == 2 and len(set(keys)) == 1  # the provider deduplicates on the same key


def test_without_provider_lookup_an_ambiguous_send_is_never_resent(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    harness.provider.supports_lookup = False  # like real SES
    message_id = _internal(harness, tenant)
    harness.provider.modes = ["ambiguous"]
    harness.send_pending(tenant)
    soon = datetime.now(UTC) + timedelta(seconds=harness.settings.stuck_sending_seconds + 5)
    assert harness.run(lambda d: sweep_tenant(d, tenant, now=soon)).get("skipped") == 1
    much_later = datetime.now(UTC) + timedelta(seconds=harness.settings.ambiguous_give_up_seconds + 5)
    assert harness.run(lambda d: sweep_tenant(d, tenant, now=much_later))["failed"] == 1
    row = harness.message(message_id, tenant)
    assert row["state"] == "failed" and row["last_error"] == "ambiguous_unconfirmed"
    assert len(harness.provider.sent) == 1


def test_transient_errors_back_off_and_are_bounded_permanent_are_not_retried(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    harness.settings.send_max_attempts = 2
    message_id = _internal(harness, tenant)
    harness.provider.modes = ["transient", "transient"]
    [first] = harness.send_pending(tenant)
    assert first.status == "deferred" and 24 <= first.delay <= 36  # base 30 s with +-20 % jitter
    harness.sql("UPDATE email_messages SET next_attempt_at = NULL WHERE id = :i", {"i": message_id}, tenant_id=tenant)
    [second] = harness.send_pending(tenant)
    assert second.status == "failed" and second.detail == "retries_exhausted"
    permanent = _internal(harness, tenant, to="p@example.test")
    harness.provider.modes = ["permanent"]
    [outcome] = harness.send_pending(tenant)
    assert outcome.status == "failed" and outcome.detail == "provider_MessageRejected"
    assert harness.message(permanent, tenant)["attempts"] == 1 and not harness.enqueued


def test_sweeper_republishes_lost_jobs(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    message_id = _internal(harness, tenant)
    harness.enqueued.clear()  # the publish after commit was lost
    later = datetime.now(UTC) + timedelta(seconds=harness.settings.stuck_queued_seconds + 5)
    assert harness.run(lambda d: sweep_tenant(d, tenant, now=later))["republished"] == 1
    assert harness.enqueued[0][3]["message_id"] == message_id


def test_unverified_sender_domain_fails_at_send_time(harness, tenants):
    tenant, _ = tenants
    harness.enqueued.clear()
    message_id = _internal(harness, tenant)
    harness.sql("UPDATE sending_domains SET status = 'failed'", tenant_id=tenant)
    [outcome] = harness.send_pending(tenant)
    assert outcome.detail == "domain_not_verified" and harness.message(message_id, tenant)["state"] == "failed"
