"""Audience snapshot (`calls.schedule` task): page crm `segments/{id}/resolve` (channel phone, contactable only)
into `campaign_contacts` with an idempotent upsert. The cursor is saved after every page, so a crashed or
redelivered task resumes where it stopped and never duplicates a contact."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from campaign_service.domain import Deps, load_campaign, record, sync_registry, transition
from campaign_service.tables import call_campaigns, campaign_contacts, dispatch_registry
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.clients import UpstreamError, UpstreamUnavailable
from nexus_core.context import Actor

log = logging.getLogger(__name__)
SYSTEM = Actor("system", "campaign-snapshot")


async def snapshot_audience(
    deps: Deps, ctx: JobContext, campaign_id: uuid.UUID, *, page_size: int = 500, stale_seconds: int = 300
) -> str:
    tenant_id = ctx.tenant_id
    stale = datetime.now(UTC) - timedelta(seconds=stale_seconds)
    async with deps.db.tenant_session(tenant_id, SYSTEM) as s:
        claimed = (
            await s.execute(
                update(call_campaigns)
                .where(
                    call_campaigns.c.id == campaign_id,
                    call_campaigns.c.state.in_(("running", "paused")),
                    or_(
                        call_campaigns.c.snapshot_status == "pending",
                        (call_campaigns.c.snapshot_status == "running")
                        & (call_campaigns.c.snapshot_updated_at < stale),
                    ),
                )
                .values(snapshot_status="running", snapshot_updated_at=func.now())
                .returning(call_campaigns.c.segment_id, call_campaigns.c.snapshot_cursor)
            )
        ).first()
        if claimed is None:
            return "skipped"
        await sync_registry(s, await load_campaign(s, campaign_id))
    segment_id, cursor = claimed.segment_id, claimed.snapshot_cursor
    crm = deps.client("crm-service")
    while True:
        try:
            page = await crm.post_json(
                f"/internal/v1/segments/{segment_id}/resolve",
                {"cursor": cursor, "limit": page_size, "channel": "phone"},
                tenant_id=tenant_id,
                actor=ctx.actor,
            )
        except UpstreamUnavailable as exc:
            raise TransientFailure("crm unavailable") from exc
        except UpstreamError as exc:
            if exc.upstream_status >= 500:
                raise TransientFailure("crm error") from exc
            async with deps.db.tenant_session(tenant_id, SYSTEM) as s:
                c = await load_campaign(s, campaign_id, lock=True)
                await s.execute(
                    update(call_campaigns).where(call_campaigns.c.id == campaign_id).values(snapshot_status="failed")
                )
                c["snapshot_status"] = "failed"
                await transition(s, c, "fail", actor=SYSTEM, values={"failure_reason": "segment_unavailable"})
            return "failed"
        ids = [uuid.UUID(str(item["contact_id"])) for item in page.get("items", [])]
        next_cursor = page.get("next_cursor")
        async with deps.db.tenant_session(tenant_id, SYSTEM) as s:
            state = (
                await s.execute(
                    select(call_campaigns.c.state).where(call_campaigns.c.id == campaign_id).with_for_update()
                )
            ).scalar_one()
            if state not in ("running", "paused"):
                return "stopped"  # canceled meanwhile: stop adding contacts
            inserted = 0
            if ids:
                inserted = len(
                    (
                        await s.execute(
                            insert(campaign_contacts)
                            .values(
                                [{"tenant_id": tenant_id, "campaign_id": campaign_id, "contact_id": i} for i in ids]
                            )
                            .on_conflict_do_nothing(constraint="uq_campaign_contacts_member")
                            .returning(campaign_contacts.c.id)
                        )
                    ).all()
                )
            done = next_cursor is None
            await s.execute(
                update(call_campaigns)
                .where(call_campaigns.c.id == campaign_id)
                .values(
                    snapshot_cursor=next_cursor,
                    snapshot_total=call_campaigns.c.snapshot_total + inserted,
                    snapshot_updated_at=func.now(),
                    snapshot_status="completed" if done else "running",
                )
            )
            await s.execute(
                update(dispatch_registry)
                .where(dispatch_registry.c.campaign_id == campaign_id)
                .values(snapshot_status="completed" if done else "running", updated_at=func.now())
            )
            if done:
                total = (
                    await s.execute(select(call_campaigns.c.snapshot_total).where(call_campaigns.c.id == campaign_id))
                ).scalar_one()
                await record(
                    s,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    kind="snapshot_completed",
                    actor=SYSTEM,
                    data={"contacts": int(total)},
                )
                return "completed"
        cursor = next_cursor
