"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): the analytics read model for 30 days.

* Acme: one fact row per showcase call, booking, email, AI draft, AI request, new lead and launched campaign,
  and `daily_metrics` computed from exactly those rows, so the dashboard matches the call, booking, email and
  inbox pages.
* Globex and the platform tenants: 30 days of synthetic `daily_metrics` (no facts), so the platform usage page
  and the Globex analytics page have history.

Idempotent: facts use fixed ids (ON CONFLICT DO NOTHING) and every (day, metric) counter in the window is set,
not added (missing ones are set to 0), so a second run changes nothing. Run it after `seed_dev.py`: that seed
sets its own 14-day demo counters. Runs as `analytics_app`, so RLS applies.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from analytics_service.projections import METRICS
from analytics_service.tables import (
    daily_metrics,
    fact_ai_usage,
    fact_bookings,
    fact_calls,
    fact_campaigns,
    fact_drafts,
    fact_emails,
    fact_leads,
)
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import GLOBEX
from nexus_core.devshowcase import (
    GLOBEX_PROFILE,
    PLATFORM_TENANTS,
    TENANT,
    WINDOW_DAYS,
    Scenario,
    acme_ai_calls,
    tenant_daily,
)

SEED_ACTOR = Actor("system", "dev-showcase")


async def _set_metrics(
    s: AsyncSession, tenant_id: uuid.UUID, anchor: date, values: dict[tuple[date, str, str], int]
) -> None:
    rows = dict(values)
    for i in range(WINDOW_DAYS):
        d = anchor - timedelta(days=i)
        for metric in METRICS:
            rows.setdefault((d, metric, ""), 0)
    items = sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2]))
    for start in range(0, len(items), 500):
        chunk = [{"tenant_id": tenant_id, "day": d, "metric": m, "dimension": dim, "value": v}
                 for (d, m, dim), v in items[start : start + 500]]  # fmt: skip
        stmt = insert(daily_metrics).values(chunk)
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "day", "metric", "dimension"],
                set_={"value": stmt.excluded.value},
                where=daily_metrics.c.value != stmt.excluded.value,
            )
        )


async def seed(db: Database, sc: Scenario) -> None:  # noqa: PLR0912 - one block per fact table
    tid = TENANT.id
    async with db.tenant_session(tid, SEED_ACTOR) as s:
        for c in sc.calls:
            await s.execute(
                insert(fact_calls)
                .values(
                    id=c.id, tenant_id=tid, campaign_id=c.campaign.id if c.campaign else None, contact_id=c.contact.id,
                    direction=c.direction, state=c.state, state_at=c.ended_at, final_state=c.final_state,
                    answered=c.answered_at is not None, completed=True, opted_out=c.opted_out,
                    duration_seconds=c.duration, cost_micros=c.cost_micros,
                    outcome=c.outcome if c.has_session else None,
                    qualified=c.qualified if c.has_session else None, first_seen_at=c.created_at,
                    completed_at=c.ended_at, created_at=c.created_at, updated_at=c.ended_at,
                )  # fmt: skip
                .on_conflict_do_nothing()
            )
        for b in sc.bookings:
            await s.execute(
                insert(fact_bookings)
                .values(id=b.id, tenant_id=tid, contact_id=b.contact.id, status=b.status, confirmed=True,
                        canceled=b.status == "canceled", first_seen_at=b.created_at, created_at=b.created_at,
                        updated_at=b.updated_at or b.created_at)  # fmt: skip
                .on_conflict_do_nothing()
            )
        for camp in sc.email_campaigns:
            await s.execute(
                insert(fact_campaigns)
                .values(id=camp.id, tenant_id=tid, channel="email", launched_at=camp.launched_at,
                        created_at=camp.launched_at)  # fmt: skip
                .on_conflict_do_nothing()
            )
            for m in camp.messages:
                await s.execute(
                    insert(fact_emails)
                    .values(
                        id=m.id, tenant_id=tid, campaign_id=camp.id, category="campaign", state=m.state,
                        state_at=m.state_changed_at, accepted=True, delivered=m.delivered_at is not None,
                        bounced=m.state == "bounced", complained=m.state == "complained",
                        unsubscribed=m.state == "unsubscribed", first_seen_at=m.queued_at, created_at=m.queued_at,
                        updated_at=m.state_changed_at,
                    )  # fmt: skip
                    .on_conflict_do_nothing()
                )
        for tx in sc.tx_emails:
            await s.execute(
                insert(fact_emails)
                .values(id=tx.id, tenant_id=tid, category=tx.category, state="delivered", state_at=tx.delivered_at,
                        accepted=True, delivered=True, first_seen_at=tx.accepted_at, created_at=tx.accepted_at,
                        updated_at=tx.delivered_at)  # fmt: skip
                .on_conflict_do_nothing()
            )
        for cc in sc.call_campaigns:
            if cc.launched_at:
                await s.execute(
                    insert(fact_campaigns)
                    .values(id=cc.id, tenant_id=tid, channel="call", launched_at=cc.launched_at,
                            created_at=cc.launched_at)  # fmt: skip
                    .on_conflict_do_nothing()
                )
        for th in sc.threads:
            if th.draft_id is None:
                continue
            acts = {a.action for a in th.draft_actions}
            await s.execute(
                insert(fact_drafts)
                .values(
                    id=th.draft_id, tenant_id=tid, thread_id=th.id, status=th.draft_status, generated_version=1,
                    approved_version=th.approved_version, generated=True, approved="approved" in acts,
                    edited="approved" in acts and (th.approved_version or 1) > 1, rejected="rejected" in acts,
                    sent="sent" in acts, first_seen_at=th.draft_created_at, created_at=th.draft_created_at,
                    updated_at=th.draft_updated_at,
                )  # fmt: skip
                .on_conflict_do_nothing()
            )
        for ct in sc.contacts:
            if ct.new_lead:
                await s.execute(
                    insert(fact_leads)
                    .values(id=ct.id, tenant_id=tid, lead_id=ct.lead_id, occurred_at=ct.created_at,
                            created_at=ct.created_at)  # fmt: skip
                    .on_conflict_do_nothing()
                )
        ai = acme_ai_calls(sc)
        for start in range(0, len(ai), 200):
            await s.execute(
                insert(fact_ai_usage)
                .values([
                    {"id": a.id, "tenant_id": tid, "model": a.model, "provider": a.provider, "purpose": a.purpose,
                     "input_tokens": a.input_tokens, "output_tokens": a.output_tokens, "cost_micros": a.cost_micros,
                     "latency_ms": a.latency_ms, "occurred_at": a.at, "created_at": a.at}
                    for a in ai[start : start + 200]
                ])  # fmt: skip
                .on_conflict_do_nothing()
            )
        await _set_metrics(s, tid, sc.anchor, sc.daily_metrics)
    for profile, tenant_id in ((GLOBEX_PROFILE, GLOBEX.id), *((t, t.id) for t in PLATFORM_TENANTS)):
        async with db.tenant_session(tenant_id, SEED_ACTOR) as s:
            await _set_metrics(s, tenant_id, sc.anchor, tenant_daily(profile, sc.anchor))
