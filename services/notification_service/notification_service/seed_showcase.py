"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): in-app notifications for the Acme demo users,
each pointing at a real showcase object (pending AI drafts, the escalated complaint, human handoffs, finished
or paused campaigns, tomorrow's bookings, the failed webhook delivery, a credential due for rotation), plus a
couple for the Globex owner. Some are already read by the users they concern.

Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as `notify_app`, so RLS applies. Links use the web app's
real routes (`/app/...`). Nothing is emailed: rows are inserted directly, not through the consumers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, DEMO_USERS, GLOBEX
from nexus_core.devshowcase import Scenario, local, sid
from notification_service.domain import KIND_RE, LINK_RE, PERMISSION_RE
from notification_service.tables import notification_reads, notifications

SEED_ACTOR = Actor("system", "dev-showcase")
_USER = {u.email: u.id for u in DEMO_USERS}
OWNER, ADMIN = _USER["owner@acme.test"], _USER["admin@acme.test"]
CAMPAIGNS, AGENT, EMAIL = _USER["campaigns@acme.test"], _USER["agent@acme.test"], _USER["email@acme.test"]
GLOBEX_OWNER = _USER["owner@globex.test"]
# Ids shared with integration_service.seed_showcase (same deterministic namespace).
WEBHOOK_ENDPOINT_ID = sid("webhook_endpoint", 1)
HUBSPOT_CONNECTION_ID = sid("provider_connection", 1)


@dataclass(frozen=True, slots=True)
class Row:
    n: int
    tenant_id: uuid.UUID
    kind: str
    title: str
    body: str
    link: str | None
    created_at: datetime
    user_id: uuid.UUID | None = None
    permission: str | None = None
    read_by: tuple[tuple[uuid.UUID, timedelta], ...] = ()  # (user, read after)


def _rows(sc: Scenario) -> list[Row]:
    rows: list[Row] = []

    def add(**kw: object) -> None:
        rows.append(Row(n=len(rows) + 1, **kw))  # type: ignore[arg-type]

    pending = sorted((t for t in sc.threads if t.draft_status == "pending_review"), key=lambda t: t.n)
    for th in pending[-4:]:
        gen = next(a.created_at for a in th.draft_actions if a.action == "generated")
        add(tenant_id=ACME.id, kind="draft_ready", title="AI reply draft ready for review",
            body="A new AI-generated reply draft is waiting for review. Nothing is sent until it is approved.",
            link=f"/app/inbox/drafts/{th.draft_id}", created_at=gen + timedelta(seconds=2),
            permission="emails:drafts:review",
            read_by=((EMAIL, timedelta(minutes=25)),) if th is pending[-4] else ())  # fmt: skip
    for th in pending:
        esc = next((a for a in th.draft_actions if a.action == "escalated"), None)
        if esc is not None:
            add(tenant_id=ACME.id, kind="draft_escalated", title="Email draft escalated",
                body="An AI reply draft was escalated for review by a senior team member.",
                link=f"/app/inbox/drafts/{th.draft_id}", created_at=esc.created_at + timedelta(seconds=1),
                permission="emails:drafts:approve")  # fmt: skip
    handoffs = sorted((c for c in sc.calls if c.handoff_at), key=lambda c: c.handoff_at)  # type: ignore[arg-type,return-value]
    for i, c in enumerate(handoffs[-3:]):
        assert c.handoff_at is not None
        add(tenant_id=ACME.id, kind="handoff_requested", title="A caller asked for a human",
            body="A live call needs a human agent.", link=f"/app/calls/{c.id}",
            created_at=c.handoff_at + timedelta(seconds=1), permission="calls:manage",
            read_by=((ADMIN, timedelta(minutes=2)),) if i < 2 else ())  # fmt: skip
    for camp in sc.call_campaigns:
        reached = sum(1 for m in camp.members if m.state == "completed")
        if camp.state == "completed" and camp.completed_at:
            add(tenant_id=ACME.id, kind="generic", title="Call campaign completed",
                body=f"{camp.name} finished: {camp.snapshot_total} contacts in the audience, {reached} reached.",
                link=f"/app/campaigns/calls/{camp.id}", created_at=camp.completed_at + timedelta(seconds=5),
                user_id=CAMPAIGNS, read_by=((CAMPAIGNS, timedelta(hours=14)),))  # fmt: skip
        elif camp.state == "paused" and camp.paused_at:
            add(tenant_id=ACME.id, kind="generic", title="Call campaign paused",
                body=f"{camp.name} is paused with {reached} of {camp.snapshot_total} contacts reached. Resume it "
                "when the crew has capacity again.", link=f"/app/campaigns/calls/{camp.id}",
                created_at=camp.paused_at + timedelta(seconds=5), user_id=CAMPAIGNS,
                read_by=((CAMPAIGNS, timedelta(minutes=40)),))  # fmt: skip
    for ec in sc.email_campaigns:
        if ec.completed_at:
            delivered = sum(1 for m in ec.messages if m.delivered_at)
            add(tenant_id=ACME.id, kind="generic", title="Email campaign sent",
                body=f"{ec.name} was sent to {len(ec.messages)} recipients; {delivered} delivered.",
                link=f"/app/campaigns/email/{ec.id}", created_at=ec.completed_at + timedelta(seconds=5),
                user_id=CAMPAIGNS,
                read_by=((CAMPAIGNS, timedelta(hours=3)),) if ec is not sc.email_campaigns[1] else ())  # fmt: skip
    eve = local(sc.anchor - timedelta(days=1), 17, 0)
    upcoming = sorted((b for b in sc.bookings if b.status == "confirmed" and b.start > eve), key=lambda b: b.start)
    for k, b in enumerate(upcoming[:2]):
        add(tenant_id=ACME.id, kind="booking_reminder", title="Upcoming booking",
            body=f"{b.service} for {b.contact.first} {b.contact.last[0]}. is coming up soon.",
            link="/app/bookings", created_at=eve - timedelta(minutes=40 - 25 * k), user_id=AGENT)  # fmt: skip
    failed_at = local(sc.anchor - timedelta(days=6), 15, 42) + timedelta(seconds=1)  # integration seed: final attempt
    add(tenant_id=ACME.id, kind="webhook_failed", title="Webhook delivery failed",
        body="A webhook delivery failed after 6 attempts and will not be retried automatically.",
        link=f"/app/integrations/webhooks/{WEBHOOK_ENDPOINT_ID}", created_at=failed_at,
        permission="integrations:manage", read_by=((ADMIN, timedelta(hours=2)),))  # fmt: skip
    add(tenant_id=ACME.id, kind="credential_rotation_due", title="Credential rotation due",
        body="A provider credential is due for rotation.",
        link=f"/app/integrations/connections/{HUBSPOT_CONNECTION_ID}",
        created_at=local(sc.anchor - timedelta(days=2), 9, 0), permission="integrations:manage")  # fmt: skip
    add(tenant_id=GLOBEX.id, kind="generic", title="Weekly summary ready",
        body="Your workspace analytics for last week are ready to review.", link="/app/analytics",
        created_at=local(sc.anchor - timedelta(days=3), 8, 0), user_id=GLOBEX_OWNER)  # fmt: skip
    add(tenant_id=GLOBEX.id, kind="draft_ready", title="AI reply draft ready for review",
        body="A new AI-generated reply draft is waiting for review. Nothing is sent until it is approved.",
        link="/app/inbox", created_at=local(sc.anchor - timedelta(days=1), 10, 12),
        permission="emails:drafts:review")  # fmt: skip
    return rows


async def seed(db: Database, sc: Scenario) -> None:
    rows = _rows(sc)
    for r in rows:  # same checks create_notification() applies
        assert KIND_RE.match(r.kind) and (r.link is None or LINK_RE.match(r.link)), r
        assert (r.user_id is None) != (r.permission is None), r
        assert r.permission is None or PERMISSION_RE.match(r.permission), r
    for tenant_id in (ACME.id, GLOBEX.id):
        async with db.tenant_session(tenant_id, SEED_ACTOR) as s:
            for r in (r for r in rows if r.tenant_id == tenant_id):
                nid = sid("notification", r.n)
                await s.execute(
                    insert(notifications)
                    .values(
                        id=nid,
                        tenant_id=tenant_id,
                        user_id=r.user_id,
                        audience_permission=r.permission,
                        kind=r.kind,
                        title=r.title,
                        body=r.body,
                        link=r.link,
                        dedupe_key=f"showcase:{nid}",
                        source="dev-showcase",
                        created_at=r.created_at,
                    )
                    .on_conflict_do_nothing()
                )
                for user_id, after in r.read_by:
                    await s.execute(
                        insert(notification_reads)
                        .values(tenant_id=tenant_id, notification_id=nid, user_id=user_id,
                                read_at=r.created_at + after)  # fmt: skip
                        .on_conflict_do_nothing()
                    )
