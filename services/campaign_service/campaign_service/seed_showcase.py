"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): six Acme call campaigns (completed, running,
paused, scheduled, draft), their audience snapshots, per-contact progress and dispatch history.

Idempotent (fixed ids, ON CONFLICT DO NOTHING; append-only history is written only with its campaign). Runs as
`campaign_app`, so RLS applies. Nothing is put in `dispatch_registry`, so the dispatcher never picks these
campaigns up: seeding cannot place a call. (Pausing and resuming one in the UI would register it.)
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from campaign_service.tables import call_campaigns, campaign_contacts, campaign_events
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devshowcase import CAMPAIGN_MANAGER, TENANT, VOICE_AGENT_ID, Scenario

SEED_ACTOR = Actor("system", "dev-showcase")


async def seed(db: Database, sc: Scenario) -> None:
    mgr = f"user:{CAMPAIGN_MANAGER}"
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        for camp in sc.call_campaigns:
            created = (
                await s.execute(
                    insert(call_campaigns)
                    .values(
                        id=camp.id,
                        tenant_id=TENANT.id,
                        name=camp.name,
                        description=camp.description,
                        agent_id=VOICE_AGENT_ID,
                        segment_id=camp.segment_id,
                        state=camp.state,
                        timezone=TENANT.timezone,
                        window_start=camp.window_start,
                        window_end=camp.window_end,
                        window_days=list(camp.window_days),
                        max_concurrency=camp.max_concurrency,
                        calls_per_minute=camp.calls_per_minute,
                        max_attempts=camp.max_attempts,
                        retry_delay_seconds=camp.retry_delay_seconds,
                        start_at=camp.start_at,
                        end_at=camp.end_at,
                        snapshot_status=camp.snapshot_status,
                        snapshot_total=camp.snapshot_total,
                        snapshot_updated_at=camp.members[0].created_at if camp.members else None,
                        launched_at=camp.launched_at,
                        launched_by=mgr if camp.launched_at else None,
                        completed_at=camp.completed_at,
                        created_by=mgr,
                        version=camp.version,
                        created_at=camp.created_at,
                        updated_at=camp.updated_at or camp.created_at,
                    )
                    .on_conflict_do_nothing()
                    .returning(call_campaigns.c.id)
                )
            ).scalar_one_or_none()
            for m in camp.members:
                await s.execute(
                    insert(campaign_contacts)
                    .values(
                        id=m.id,
                        tenant_id=TENANT.id,
                        campaign_id=camp.id,
                        contact_id=m.contact.id,
                        state=m.state,
                        attempts=m.attempts,
                        next_attempt_at=m.next_attempt_at,
                        last_call_id=m.last_call_id,
                        outcome=m.outcome,
                        skip_reason=m.skip_reason,
                        dispatched_at=m.dispatched_at,
                        completed_at=m.completed_at,
                        version=1 + 2 * m.attempts,
                        created_at=m.created_at,
                        updated_at=m.updated_at or m.created_at,
                    )
                    .on_conflict_do_nothing()
                )
            if created is None:
                continue  # already seeded: the append-only event history is never duplicated
            for e in camp.events:
                await s.execute(
                    insert(campaign_events)
                    .values(
                        id=e.id,
                        tenant_id=TENANT.id,
                        campaign_id=camp.id,
                        kind=e.kind,
                        from_state=e.from_state,
                        to_state=e.to_state,
                        contact_id=e.contact_id,
                        call_id=e.call_id,
                        actor=e.actor,
                        data=e.data,
                        created_at=e.created_at,
                    )
                    .on_conflict_do_nothing()
                )
