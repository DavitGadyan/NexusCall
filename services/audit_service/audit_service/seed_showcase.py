"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): a month of audit records that match the other
showcase data: Acme campaign, email, AI-draft, booking, transcript, team and integration activity; a little
Globex administration; the onboarding of each platform tenant (and the suspension of one); and platform-level
model, routing, feature-flag and RLS-scan changes. Action names, resource types and producers are the ones the
services really emit.

Idempotent: deterministic event ids, and `append` skips an event id it already recorded, so a re-run adds
nothing and the hash chains stay valid. Runs as `audit_app` (RLS applies); platform records are appended from
a session without tenant context, as the platform chain requires.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from audit_service.chain import AuditRecord, append
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, DEMO_USERS, GLOBEX
from nexus_core.devshowcase import PLATFORM_TENANTS, Scenario, local, platform_user_id, sid

SEED_ACTOR = Actor("system", "dev-showcase")
_USER = {u.email: u.id for u in DEMO_USERS}
PLATFORM_OWNER, PLATFORM_OPERATOR = _USER["platform.owner@nexus.local"], _USER["platform.operator@nexus.local"]
OWNER, ADMIN = _USER["owner@acme.test"], _USER["admin@acme.test"]
CAMPAIGNS, AGENT, EMAIL = _USER["campaigns@acme.test"], _USER["agent@acme.test"], _USER["email@acme.test"]
GLOBEX_OWNER = _USER["owner@globex.test"]
# Ids shared with integration_service.seed_showcase.
HUBSPOT_CONNECTION_ID, CALENDAR_CONNECTION_ID = sid("provider_connection", 1), sid("provider_connection", 2)
WEBHOOK_ENDPOINT_ID = sid("webhook_endpoint", 1)


class _Log:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def add(self, tenant_id: uuid.UUID | None, at: datetime, actor: tuple[str, object], action: str,
            resource_type: str, resource_id: object, producer: str, outcome: str = "success",
            **metadata: Any) -> None:  # fmt: skip
        self.records.append(
            AuditRecord(
                event_id=sid("audit_event", len(self.records) + 1),
                tenant_id=tenant_id,
                occurred_at=at,
                actor_type=actor[0],
                actor_id=str(actor[1]),
                action=action,
                resource_type=resource_type,
                resource_id=None if resource_id is None else str(resource_id),
                outcome=outcome,
                request_id=None,
                correlation_id=None,
                causation_id=None,
                producer=producer,
                metadata=metadata,
            )
        )


def _user(user_id: uuid.UUID) -> tuple[str, object]:
    return ("user", user_id)


def records(sc: Scenario) -> list[AuditRecord]:  # noqa: PLR0912, PLR0915 - one block per kind of activity
    log, a = _Log(), ACME.id

    def day(n: int, h: int, m: int = 0) -> datetime:
        return local(sc.anchor - timedelta(days=n), h, m)

    # ---------------------------------------------------------------- Acme: team, settings, integrations
    log.add(a, day(29, 9, 12), _user(OWNER), "tenant.settings.updated", "tenant_settings", "communication",
            "tenant-service", section="communication", version=2)  # fmt: skip
    log.add(a, day(28, 10, 3), _user(ADMIN), "invitation.created", "invitation", sid("invitation", 1),
            "tenant-service", roles=["call_agent"])  # fmt: skip
    log.add(a, day(28, 16, 40), _user(AGENT), "invitation.accepted", "invitation", sid("invitation", 1),
            "tenant-service", roles=["call_agent"])  # fmt: skip
    log.add(a, day(27, 11, 5), _user(ADMIN), "membership.updated", "membership", sid("membership", 104),
            "tenant-service", roles=["call_agent"])  # fmt: skip
    log.add(a, day(80, 14, 21), _user(ADMIN), "integration.connection.created", "provider_connection",
            HUBSPOT_CONNECTION_ID, "integration-service", provider="hubspot")  # fmt: skip
    log.add(a, day(26, 14, 22), _user(ADMIN), "integration.connection.created", "provider_connection",
            CALENDAR_CONNECTION_ID, "integration-service", provider="google_calendar")  # fmt: skip
    log.add(a, day(25, 9, 48), _user(ADMIN), "webhook.endpoint.created", "webhook_endpoint", WEBHOOK_ENDPOINT_ID,
            "integration-service", event_types=["booking.canceled", "booking.confirmed", "booking.created",
                                                "lead.created"])  # fmt: skip
    log.add(a, day(25, 10, 2), _user(OWNER), "ai.budget.updated", "ai_budget", "tenant", "ai-gateway",
            monthly_limit_usd=150.0)  # fmt: skip
    log.add(a, day(14, 17, 25), _user(OWNER), "tenant.settings.updated", "tenant_settings", "retention",
            "tenant-service", section="retention", version=2)  # fmt: skip
    log.add(a, day(8, 9, 30), _user(ADMIN), "booking.resource.updated", "booking_resource",
            sid("resource", 1), "booking-service", fields=["name"])  # fmt: skip

    # ---------------------------------------------------------------- Acme: call campaigns
    mgr = _user(CAMPAIGNS)
    for camp in sc.call_campaigns:
        log.add(a, camp.created_at, mgr, "campaign.created", "call_campaign", camp.id, "campaign-service")
        for e in camp.events:
            if e.kind == "state.schedule":
                log.add(a, e.created_at, mgr, "campaign.schedule", "call_campaign", camp.id, "campaign-service",
                        to_state="scheduled")  # fmt: skip
            elif e.kind == "state.launch":
                log.add(a, e.created_at, mgr, "campaign.launched", "call_campaign", camp.id, "campaign-service")
            elif e.kind == "state.pause":
                log.add(a, e.created_at, mgr, "campaign.pause", "call_campaign", camp.id, "campaign-service",
                        to_state="paused")  # fmt: skip

    # ---------------------------------------------------------------- Acme: email campaigns and templates
    for i, ec in enumerate(sc.email_campaigns, start=1):
        log.add(a, ec.created_at - timedelta(days=1), mgr, "email_template.created", "email_template",
                sid("email_template", i), "email-service")  # fmt: skip
        log.add(a, ec.created_at, mgr, "email_campaign.created", "email_campaign", ec.id, "email-service")
        log.add(a, ec.launched_at, mgr, "email_campaign.launched", "email_campaign", ec.id, "email-service")
        if ec.completed_at:
            log.add(a, ec.completed_at, ("system", "email-scheduler"), "email_campaign.completed", "email_campaign",
                    ec.id, "email-service", reason="sent")  # fmt: skip

    # ---------------------------------------------------------------- Acme: AI drafts (human review)
    for th in sc.threads:
        for act in th.draft_actions:
            if act.action == "approved":
                log.add(a, act.created_at, ("user", act.actor_id), "email.draft.approved", "email_draft", th.draft_id,
                        "email-inbox-service", version=act.version, escalated=th.escalated)  # fmt: skip
            elif act.action == "sent":
                log.add(a, act.created_at, ("system", "email-sender"), "email.reply.sent", "email_draft",
                        th.draft_id, "email-inbox-service", version=act.version,
                        approved_by=th.approved_by)  # fmt: skip
            elif act.action == "rejected":
                log.add(a, act.created_at, ("user", act.actor_id), "email.draft.rejected", "email_draft",
                        th.draft_id, "email-inbox-service", reason=act.reason)  # fmt: skip
            elif act.action == "escalated":
                log.add(a, act.created_at, ("user", act.actor_id), "email.draft.escalated", "email_draft",
                        th.draft_id, "email-inbox-service", reason=act.reason)  # fmt: skip

    # ---------------------------------------------------------------- Acme: bookings, calls, transcripts
    for b in sc.bookings:
        if b.source_type == "user":
            log.add(a, b.created_at, ("user", b.created_by.split(":", 1)[1]), "booking.created", "booking", b.id,
                    "booking-service", status="confirmed")  # fmt: skip
        if b.status == "canceled" and b.canceled_at:
            log.add(a, b.canceled_at, _user(AGENT), "booking.canceled", "booking", b.id, "booking-service",
                    reason=b.cancel_reason)  # fmt: skip
    for c in sc.calls:
        if c.opted_out and c.ended_at:
            log.add(a, c.ended_at, ("service", "voice-service"), "voice.opt_out.detected", "call", c.id,
                    "voice-service")  # fmt: skip
    reviewed = [c for c in sc.calls if c.handoff_state or c.opted_out]
    for k, c in enumerate(reviewed[-6:]):
        assert c.ended_at is not None
        who = (OWNER, ADMIN, CAMPAIGNS)[k % 3]
        log.add(a, c.ended_at + timedelta(hours=2 + k), _user(who), "call.transcript.accessed", "call", c.id,
                "voice-service")  # fmt: skip

    # ---------------------------------------------------------------- Globex
    g = GLOBEX.id
    log.add(g, day(21, 10, 15), _user(GLOBEX_OWNER), "tenant.settings.updated", "tenant_settings", "profile",
            "tenant-service", section="profile", version=2)  # fmt: skip
    log.add(g, day(12, 13, 2), _user(GLOBEX_OWNER), "invitation.created", "invitation", sid("invitation", 2),
            "tenant-service", roles=["email_manager"])  # fmt: skip
    log.add(g, day(4, 11, 47), _user(GLOBEX_OWNER), "ai.budget.updated", "ai_budget", "tenant", "ai-gateway",
            monthly_limit_usd=60.0)  # fmt: skip

    # ---------------------------------------------------------------- platform tenants (tenant chains)
    for t in PLATFORM_TENANTS:
        created = local(sc.anchor - timedelta(days=t.created_days_ago), 15, 10)
        owner = platform_user_id(t, 0)
        log.add(t.id, created, _user(PLATFORM_OWNER), "tenant.created", "tenant", t.id, "tenant-service", slug=t.slug)
        log.add(t.id, created + timedelta(minutes=1), _user(PLATFORM_OWNER), "invitation.created", "invitation",
                sid("invitation", 100 + t.n), "tenant-service", roles=["tenant_owner"])  # fmt: skip
        log.add(t.id, created + timedelta(hours=3), _user(owner), "invitation.accepted", "invitation",
                sid("invitation", 100 + t.n), "tenant-service", roles=["tenant_owner"])  # fmt: skip
        if t.status == "suspended":
            log.add(t.id, day(9, 16, 5), _user(PLATFORM_OPERATOR), "tenant.suspended", "tenant", t.id,
                    "tenant-service", previous="active", reason="Trial ended without a signed agreement")  # fmt: skip
        else:
            log.add(t.id, day(2 + t.n % 5, 9 + t.n, 30), _user(owner), "tenant.settings.updated", "tenant_settings",
                    "communication", "tenant-service", section="communication", version=2)  # fmt: skip

    # ---------------------------------------------------------------- platform chain
    op = _user(PLATFORM_OPERATOR)
    log.add(None, day(27, 8, 30), _user(PLATFORM_OWNER), "platform.settings.updated", "platform_settings",
            "security", "tenant-service", section="security")  # fmt: skip
    log.add(None, day(23, 11, 0), op, "ai.model.updated", "ai_model", "claude-haiku-5-5", "ai-gateway",
            fields=["max_output_tokens"])  # fmt: skip
    log.add(None, day(23, 11, 4), op, "ai.routing.updated", "ai_routing", "voice_turn", "ai-gateway",
            models=["claude-haiku-5-5", "claude-sonnet-5-5"], failover=True)  # fmt: skip
    log.add(None, day(16, 15, 20), _user(PLATFORM_OWNER), "platform.settings.updated", "platform_settings",
            "limits", "tenant-service", section="limits")  # fmt: skip
    log.add(None, day(7, 10, 10), op, "ai.model.updated", "ai_model", "claude-sonnet-5-5", "ai-gateway",
            fields=["enabled"])  # fmt: skip
    log.add(None, day(3, 9, 5), _user(PLATFORM_OWNER), "platform.usage.viewed", "usage_report", None,
            "analytics-service", tenants=len(PLATFORM_TENANTS) + 2)  # fmt: skip
    return log.records


async def seed(db: Database, sc: Scenario) -> None:
    recs = records(sc)
    for tenant_id in dict.fromkeys(r.tenant_id for r in recs if r.tenant_id is not None):
        async with db.tenant_session(tenant_id, SEED_ACTOR) as s:
            for r in sorted((r for r in recs if r.tenant_id == tenant_id), key=lambda r: r.occurred_at):
                await append(s, r)
    async with db.global_session(SEED_ACTOR) as s:
        for r in sorted((r for r in recs if r.tenant_id is None), key=lambda r: r.occurred_at):
            await append(s, r)
