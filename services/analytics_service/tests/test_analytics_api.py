"""Dashboards, exports (async job) and platform usage against a real database (RLS as analytics_app)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from analytics_service.exports import build_export, sweep_exports
from analytics_service.tables import daily_metrics, exports, outbox
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.db import Database
from nexus_core.storage import InMemoryObjectStore
from nexus_core.testing import auth

pytestmark = [pytest.mark.integration, pytest.mark.security]
TODAY = datetime.now(UTC).date()


def put_metrics(run_db, tenant, values, day=TODAY, dimension=""):
    async def go(db):
        async with db.tenant_session(tenant) as s:
            for metric, value in values.items():
                await s.execute(
                    insert(daily_metrics)
                    .values(tenant_id=tenant, day=day, metric=metric, dimension=dimension, value=value)
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "day", "metric", "dimension"], set_={"value": value}
                    )
                )

    run_db(go)


def audits(run_db, action):
    async def go(db):
        async with db.global_session() as s:
            rows = (await s.execute(select(outbox.c.envelope).where(outbox.c.event_type == "audit.event"))).scalars()
            return [r for r in rows if r["payload"]["action"] == action]

    return run_db(go)


# --------------------------------------------------------------------------- dashboards
def test_overview_rates_and_costs(harness, run_db):
    tenant = uuid.uuid4()
    put_metrics(run_db, tenant, {
        "calls.attempted": 10, "calls.answered": 6, "calls.ended": 8, "calls.completed": 6, "calls.failed": 2,
        "calls.duration_seconds": 480, "calls.cost_micros": 160_000, "emails.accepted": 100,
        "emails.delivered": 95, "emails.bounced": 4, "emails.complained": 1, "drafts.approved": 6,
        "drafts.edited": 3, "drafts.rejected": 2, "ai.requests": 20, "ai.cost_micros": 100_000,
        "bookings.created": 3,
    })  # fmt: skip
    put_metrics(run_db, tenant, {"calls.attempted": 1000}, day=TODAY - timedelta(days=60))  # outside the range
    _, tok = harness.member(tenant, {"analytics:read"})
    r = harness.client.get("/api/v1/analytics/overview", headers=auth(tok, tenant))
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["from"] == (TODAY - timedelta(days=29)).isoformat() and o["to"] == TODAY.isoformat()
    assert o["calls"]["attempted"] == 10 and o["calls"]["answer_rate"] == 0.6
    assert o["calls"]["average_duration_seconds"] == 60
    assert o["emails"]["delivery_rate"] == 0.95 and o["emails"]["complaint_rate"] == 0.01
    assert o["drafts"]["approved_rate"] == 0.75 and o["drafts"]["rejected_rate"] == 0.25
    assert o["drafts"]["edited_rate"] == 0.5
    assert o["costs"] == {
        "call_cost_micros": 160_000,
        "cost_per_call_micros": 20_000,
        "email_cost_micros": 10_000,
        "cost_per_email_micros": 100,
        "email_cost_estimated": True,
        "ai_cost_micros": 100_000,
        "cost_per_ai_interaction_micros": 5_000,
        "ai_input_tokens": 0,
        "ai_output_tokens": 0,
        "total_cost_micros": 270_000,
    }
    assert o["bookings"]["created"] == 3
    empty = harness.client.get(
        "/api/v1/analytics/overview",
        headers=auth(tok, tenant),
        params={"from": (TODAY - timedelta(days=400)).isoformat(), "to": (TODAY - timedelta(days=380)).isoformat()},
    )
    assert empty.json()["calls"]["answer_rate"] is None  # fmt: skip


def test_timeseries_is_zero_filled_and_validated(harness, run_db):
    tenant = uuid.uuid4()
    put_metrics(run_db, tenant, {"calls.attempted": 4}, day=TODAY - timedelta(days=1))
    _, tok = harness.member(tenant, {"analytics:read"})
    params = {"metric": "calls.attempted", "from": (TODAY - timedelta(days=2)).isoformat(), "to": TODAY.isoformat()}
    r = harness.client.get("/api/v1/analytics/timeseries", headers=auth(tok, tenant), params=params)
    assert r.status_code == 200
    assert [p["value"] for p in r.json()["points"]] == [0, 4, 0]
    bad = [
        {**params, "metric": "emails.opened"},
        {**params, "dimension": "contact:abc"},
        {**params, "from": params["to"], "to": params["from"]},
        {**params, "from": (TODAY - timedelta(days=400)).isoformat()},
    ]
    for p in bad:
        assert (
            harness.client.get("/api/v1/analytics/timeseries", headers=auth(tok, tenant), params=p).status_code == 422
        )


def test_campaign_drilldown_and_cross_tenant_isolation(harness, run_db):
    from analytics_service.projections import EVENT_HANDLERS
    from nexus_events import ActorRef, make_event

    a, b, campaign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def feed(db):
        for event_type, payload, producer in (
            ("campaign.launched", {"campaign_id": str(campaign), "channel": "call"}, "campaign-service"),
            ("call.completed", {"call_id": str(uuid.uuid4()), "direction": "outbound", "final_state": "completed",
                                "duration_seconds": 60, "campaign_id": str(campaign), "cost_micros": 10_000},
             "call-service"),
        ):  # fmt: skip
            e = make_event(event_type, producer=producer, actor=ActorRef(type="service", id=producer),
                           correlation_id=uuid.uuid4(), tenant_id=b, payload=payload)  # fmt: skip
            async with db.tenant_session(b) as s:
                await EVENT_HANDLERS[event_type](e, s)

    run_db(feed)
    _, tok_b = harness.member(b, {"analytics:read"})
    _, tok_a = harness.member(a, {"analytics:read", "analytics:export"})
    r = harness.client.get(f"/api/v1/analytics/campaigns/{campaign}", headers=auth(tok_b, b))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "call" and body["calls"]["attempted"] == 1 and body["calls"]["answered"] == 1
    assert body["calls"]["cost_per_call_micros"] == 10_000
    # Another tenant can neither read nor infer the campaign.
    assert harness.client.get(f"/api/v1/analytics/campaigns/{campaign}", headers=auth(tok_a, a)).status_code == 404
    assert harness.client.get(f"/api/v1/analytics/campaigns/{campaign}", headers=auth(tok_a, b)).status_code == 403
    assert harness.client.get("/api/v1/analytics/overview", headers=auth(tok_a, b)).status_code == 403
    assert harness.client.get("/api/v1/analytics/overview", headers=auth(tok_a, a)).json()["calls"]["attempted"] == 0

    async def probe(db):
        async with db.tenant_session(a) as s:
            seen = set((await s.execute(text("SELECT DISTINCT tenant_id FROM fact_calls"))).scalars())
        async with db.global_session() as s:
            none = (await s.execute(text("SELECT count(*) FROM daily_metrics"))).scalar_one()
        return seen, none

    seen, none = run_db(probe)
    assert b not in seen and none == 0


def test_permission_boundaries(harness):
    tenant = uuid.uuid4()
    _, reader = harness.member(tenant, {"analytics:read"})
    _, other = harness.member(tenant, {"calls:read"})
    assert harness.client.get("/api/v1/analytics/overview", headers=auth(other, tenant)).status_code == 403
    assert harness.client.get("/api/v1/exports", headers=auth(reader, tenant)).status_code == 403
    body = {"from": TODAY.isoformat(), "to": TODAY.isoformat()}
    r = harness.client.post("/api/v1/exports", headers={**auth(reader, tenant), "Idempotency-Key": "k"}, json=body)
    assert r.status_code == 403
    assert harness.client.get("/api/v1/platform/usage", headers=auth(reader, tenant)).status_code == 403
    assert harness.client.get("/api/v1/analytics/overview").status_code == 401


# --------------------------------------------------------------------------- exports
def _export(harness, tok, tenant, key="exp-1", **body):
    payload = {"from": (TODAY - timedelta(days=6)).isoformat(), "to": TODAY.isoformat(), **body}
    return harness.client.post("/api/v1/exports", headers={**auth(tok, tenant), "Idempotency-Key": key}, json=payload)


def test_export_requires_step_up_and_idempotency_key(harness):
    tenant = uuid.uuid4()
    _, weak = harness.member(tenant, {"analytics:export"})
    r = _export(harness, weak, tenant)
    assert r.status_code == 401 and r.json()["code"] == "step_up_required"
    _, strong = harness.member(tenant, {"analytics:export"}, mfa=True)
    no_key = harness.client.post(
        "/api/v1/exports", headers=auth(strong, tenant), json={"from": TODAY.isoformat(), "to": TODAY.isoformat()}
    )
    assert no_key.status_code == 422
    assert _export(harness, strong, tenant, metrics=["emails.opened"]).status_code == 422
    assert harness.celery.sent == []


def test_export_job_end_to_end(harness, run_db):
    tenant, other = uuid.uuid4(), uuid.uuid4()
    put_metrics(run_db, tenant, {"calls.attempted": 7, "emails.accepted": 3})
    put_metrics(run_db, tenant, {"calls.attempted": 2}, dimension=f"campaign:{uuid.uuid4()}")
    put_metrics(run_db, other, {"calls.attempted": 999})
    uid, tok = harness.member(tenant, {"analytics:export"}, mfa=True)

    r = _export(harness, tok, tenant, key="job-1")
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["download_url"] is None
    # Replay returns the stored response; the same key with another payload is rejected.
    replay = _export(harness, tok, tenant, key="job-1")
    assert replay.status_code == 202 and replay.json()["id"] == job["id"]
    assert _export(harness, tok, tenant, key="job-1", include_dimensions=True).status_code == 422
    [task] = harness.celery.sent
    assert task["name"] == "analytics.build_export" and task["queue"] == "analytics.aggregate"
    assert task["kwargs"]["export_id"] == job["id"] and task["kwargs"]["ctx"]["tenant_id"] == str(tenant)
    assert set(task["kwargs"]) == {"ctx", "export_id"}  # ids only, never data
    assert len(audits(run_db, "analytics.export.requested")) >= 1

    ctx = JobContext(**task["kwargs"]["ctx"])
    store = harness.store

    async def work(db):
        first = await build_export(db, store, ctx, uuid.UUID(job["id"]), max_rows=1000, max_attempts=3)
        again = await build_export(db, store, ctx, uuid.UUID(job["id"]), max_rows=1000, max_attempts=3)
        wrong_tenant = JobContext(tenant_id=other, correlation_id=uuid.uuid4())
        foreign = await build_export(db, store, wrong_tenant, uuid.UUID(job["id"]), max_rows=1000, max_attempts=3)
        return first, again, foreign

    assert run_db(work) == ("succeeded", "skipped", "skipped")
    [(bucket, key)] = list(store.objects)
    assert bucket == "exports" and key == f"{tenant}/analytics/{job['id']}.csv"
    csv_text = store.objects[(bucket, key)][0].decode().splitlines()
    assert csv_text[0] == "day,metric,dimension,value"
    assert sorted(csv_text[1:]) == [f"{TODAY},calls.attempted,,7", f"{TODAY},emails.accepted,,3"]

    got = harness.client.get(f"/api/v1/exports/{job['id']}", headers=auth(tok, tenant))
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "succeeded" and body["row_count"] == 2 and body["expires_in_seconds"] == 300
    assert body["download_url"].startswith(f"memory://exports/{tenant}/analytics/")
    downloads = audits(run_db, "analytics.export.downloaded")
    assert any(d["actor"]["id"] == uid and d["tenant_id"] == str(tenant) for d in downloads)
    listed = harness.client.get("/api/v1/exports", headers=auth(tok, tenant)).json()
    assert [i["id"] for i in listed["items"]] == [job["id"]]
    assert listed["items"][0]["download_url"] is None  # links are issued (and audited) only per export

    _, foreign_tok = harness.member(other, {"analytics:export"})
    assert harness.client.get(f"/api/v1/exports/{job['id']}", headers=auth(foreign_tok, other)).status_code == 404
    assert harness.client.get("/api/v1/exports", headers=auth(foreign_tok, other)).json()["items"] == []
    assert harness.client.get(f"/api/v1/exports/{job['id']}", headers=auth(foreign_tok, tenant)).status_code == 403


class BrokenStore(InMemoryObjectStore):
    async def put(self, *args, **kwargs):
        raise ConnectionError("object store down")


def test_export_storage_failure_retries_then_fails_permanently(harness, run_db):
    tenant = uuid.uuid4()
    _, tok = harness.member(tenant, {"analytics:export"}, mfa=True)
    job = _export(harness, tok, tenant, key="broken").json()
    ctx = JobContext(**harness.celery.sent[-1]["kwargs"]["ctx"])
    export_id = uuid.UUID(job["id"])

    async def attempt(db):
        return await build_export(db, BrokenStore(), ctx, export_id, max_rows=10, max_attempts=2)

    with pytest.raises(TransientFailure):
        run_db(attempt)  # attempt 1: back to queued, Celery retries with backoff
    assert run_db(attempt) == "failed"  # attempt 2 of 2: recorded as failed, no further retries
    body = harness.client.get(f"/api/v1/exports/{job['id']}", headers=auth(tok, tenant)).json()
    assert body["status"] == "failed" and body["error_code"] == "storage_unavailable" and body["download_url"] is None


def test_sweeper_requeues_stuck_jobs_and_enqueue_failure_is_not_fatal(harness, run_db, sdb):
    tenant = uuid.uuid4()
    _, tok = harness.member(tenant, {"analytics:export"}, mfa=True)
    harness.celery.fail = True
    r = _export(harness, tok, tenant, key="broker-down")
    assert r.status_code == 202  # committed even though the broker was down
    harness.celery.fail = False
    job_id = uuid.UUID(r.json()["id"])

    async def age(db):
        async with db.tenant_session(tenant) as s:
            await s.execute(
                exports.update()
                .where(exports.c.id == job_id)
                .values(updated_at=datetime.now(UTC) - timedelta(minutes=10))
            )

    run_db(age)

    async def sweep(db):
        platform = Database(sdb.url("platform"), pool_size=1, max_overflow=0)
        try:
            return await sweep_exports(db, platform, harness.celery, stale_queued_seconds=60,
                                       stale_processing_seconds=600)  # fmt: skip
        finally:
            await platform.dispose()

    assert run_db(sweep) >= 1
    assert any(t["kwargs"]["export_id"] == str(job_id) for t in harness.celery.sent)


# --------------------------------------------------------------------------- platform usage
def test_platform_usage_spans_tenants_through_the_read_only_role_and_is_audited(harness, run_db, sdb):
    a, b = uuid.uuid4(), uuid.uuid4()
    day = date(2026, 1, 15)
    put_metrics(run_db, a, {"calls.ended": 3, "calls.cost_micros": 30_000, "ai.requests": 2, "ai.cost_micros": 4_000},
                day=day)  # fmt: skip
    put_metrics(run_db, b, {"emails.accepted": 10}, day=day)
    uid, op = harness.platform_user({"platform:usage:read"})
    r = harness.client.get(
        "/api/v1/platform/usage", headers=auth(op), params={"from": "2026-01-15", "to": "2026-01-15"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    items = {i["tenant_id"]: i for i in body["items"]}
    assert items[str(a)]["total_cost_micros"] == 34_000 and items[str(a)]["calls"] == 3
    assert items[str(b)]["email_cost_micros"] == 1_000 and body["email_cost_estimated"] is True
    assert body["totals"]["tenants"] == len(items) >= 2
    viewed = audits(run_db, "platform.usage.viewed")
    assert any(v["actor"]["id"] == uid and v["tenant_id"] is None for v in viewed)
    _, no_perm = harness.platform_user({"platform:tenants:read"})
    assert harness.client.get("/api/v1/platform/usage", headers=auth(no_perm)).status_code == 403

    async def write_as_platform(db):
        async with db.global_session() as s:
            await s.execute(text("DELETE FROM daily_metrics"))

    with pytest.raises(DBAPIError, match="permission denied"):
        run_db(write_as_platform, sdb.url("platform"))
