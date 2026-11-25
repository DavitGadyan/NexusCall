"""SES event ingestion (after SNS verification): tenant routing, dedupe, state mapping, suppressions and the
deliverability auto-pause.

The tenant is resolved from `message_directory`, which this service wrote when it created the message; the
payload's own fields never select a tenant. Events are deduplicated on the SNS MessageId by a unique
constraint in `provider_events`. Opens and clicks are ignored (not tracked).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_service.deps import Deps, audit, emit
from email_service.messages import transition
from email_service.security import address_hash
from email_service.state_machine import SENT_STATES
from email_service.tables import (
    campaign_schedule_index,
    email_campaigns,
    email_messages,
    message_directory,
    provider_events,
)
from nexus_core.context import Actor

log = logging.getLogger(__name__)
INGEST = Actor("system", "ses-events")
AUTO_PAUSE_LINK = "/app/email/campaigns"


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: str  # applied | duplicate | ignored | unknown_message
    message_id: uuid.UUID | None = None
    state: str | None = None


def classify(event: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    """(event type, target state or None, extra values) for an SES event or notification."""
    kind = str(event.get("eventType") or event.get("notificationType") or "")
    if kind == "Send":
        return kind, "accepted", {}
    if kind == "Delivery":
        return kind, "delivered", {}
    if kind == "Bounce":
        bounce = event.get("bounce") or {}
        if bounce.get("bounceType") == "Permanent":
            return kind, "bounced", {"suppression_reason": "bounce", "last_error": "hard_bounce"}
        return "SoftBounce", None, {"last_error": "soft_bounce"}
    if kind == "Complaint":
        return kind, "complained", {"suppression_reason": "complaint"}
    if kind == "Reject":
        return kind, "rejected", {"last_error": "provider_reject"}
    if kind in ("Rendering Failure", "RenderingFailure"):
        return "RenderingFailure", "failed", {"last_error": "rendering_failure"}
    return kind or "Unknown", None, {}


async def _route(
    deps: Deps, provider_message_id: str, tag_message_id: str | None
) -> tuple[uuid.UUID, uuid.UUID] | None:
    async with deps.db.global_session(INGEST) as s:
        row = (
            await s.execute(
                select(message_directory.c.tenant_id, message_directory.c.message_id).where(
                    message_directory.c.provider_message_id == provider_message_id
                )
            )
        ).first()
        if row is None and tag_message_id:
            try:
                tagged = uuid.UUID(tag_message_id)
            except ValueError:
                return None
            # The tag is only a hint to find a row we created; a matching provider id is re-checked below.
            row = (
                await s.execute(
                    select(message_directory.c.tenant_id, message_directory.c.message_id).where(
                        message_directory.c.message_id == tagged,
                        message_directory.c.provider_message_id.is_(None),
                    )
                )
            ).first()
    return (row.tenant_id, row.message_id) if row else None


async def ingest_ses_event(deps: Deps, *, sns_message_id: str, event: dict[str, Any]) -> IngestResult:
    mail = event.get("mail") or {}
    provider_message_id = str(mail.get("messageId") or "")[:255]
    if not provider_message_id:
        return IngestResult("ignored")
    tags = mail.get("tags") or {}
    tag_values = tags.get("nexus_message_id") if isinstance(tags, dict) else None
    tag = str(tag_values[0]) if isinstance(tag_values, list) and tag_values else None
    routed = await _route(deps, provider_message_id, tag)
    if routed is None:
        log.info("ses_event_unknown_message", extra={"event_type": str(event.get("eventType"))})
        return IngestResult("unknown_message")
    tenant_id, message_id = routed
    kind, target, values = classify(event)
    async with deps.db.tenant_session(tenant_id, INGEST) as s:
        recorded = (
            await s.execute(
                insert(provider_events)
                .values(
                    tenant_id=tenant_id,
                    provider="ses",
                    provider_event_id=sns_message_id[:128],
                    event_type=kind[:32],
                    message_id=message_id,
                    outcome="applied" if target else "recorded",
                )
                .on_conflict_do_nothing(constraint="uq_provider_events_event")
                .returning(provider_events.c.id)
            )
        ).scalar_one_or_none()
        if recorded is None:
            return IngestResult("duplicate", message_id)
        msg = (
            (
                await s.execute(
                    select(
                        email_messages.c.id,
                        email_messages.c.state,
                        email_messages.c.campaign_id,
                        email_messages.c.contact_id,
                        email_messages.c.to_address,
                        email_messages.c.provider_message_id,
                    ).where(email_messages.c.id == message_id)
                )
            )
            .mappings()
            .first()
        )
        if msg is None:
            return IngestResult("unknown_message")
        if msg["provider_message_id"] not in (None, provider_message_id):
            return IngestResult("ignored", message_id)  # never let a tag re-point a known message
        if msg["provider_message_id"] is None:
            await s.execute(
                update(email_messages)
                .where(email_messages.c.id == message_id)
                .values(provider_message_id=provider_message_id)
            )
            await s.execute(
                update(message_directory)
                .where(message_directory.c.message_id == message_id)
                .values(provider_message_id=provider_message_id)
            )
        if target is None:
            if values:
                await s.execute(update(email_messages).where(email_messages.c.id == message_id).values(**values))
            return IngestResult("ignored", message_id, msg["state"])
        moved = await transition(s, tenant_id=tenant_id, message_id=message_id, target=target, actor=INGEST,
                                 values={**values, "ambiguous_at": None})  # fmt: skip
        if moved is None:
            if "suppression_reason" in values:  # e.g. a complaint about a message already marked unsubscribed
                await s.execute(
                    update(email_messages)
                    .where(email_messages.c.id == message_id, email_messages.c.suppression_reason.is_(None))
                    .values(suppression_reason=values["suppression_reason"])
                )
            return IngestResult("ignored", message_id, msg["state"])
        if target == "complained" and msg["to_address"]:
            await emit(
                s,
                "email.complaint",
                tenant_id=tenant_id,
                actor=INGEST,
                payload={
                    "address_hash": address_hash(msg["to_address"]),
                    "contact_id": str(msg["contact_id"]) if msg["contact_id"] else None,
                    "message_id": str(message_id),
                },
            )
        if msg["campaign_id"] is not None and target in ("bounced", "complained"):
            await maybe_auto_pause(deps, s, tenant_id, msg["campaign_id"])
    return IngestResult("applied", message_id, target)


async def maybe_auto_pause(deps: Deps, session: Any, tenant_id: uuid.UUID, campaign_id: uuid.UUID) -> str | None:
    """Pause a running campaign whose bounce or complaint rate crosses the configured threshold, and notify
    the people who manage email campaigns. Returns the pause reason when it paused."""
    settings = deps.settings
    rows = (
        await session.execute(
            select(email_messages.c.state, func.count())
            .where(email_messages.c.campaign_id == campaign_id, email_messages.c.state.in_(SENT_STATES))
            .group_by(email_messages.c.state)
        )
    ).all()
    counts = {state: int(n) for state, n in rows}
    sent = sum(counts.values())
    if sent < settings.threshold_min_sample:
        return None
    reason = None
    if counts.get("complained", 0) / sent > settings.complaint_rate_threshold:
        reason = "complaint_rate"
    elif counts.get("bounced", 0) / sent > settings.bounce_rate_threshold:
        reason = "bounce_rate"
    if reason is None:
        return None
    paused = (
        await session.execute(
            update(email_campaigns)
            .where(email_campaigns.c.id == campaign_id, email_campaigns.c.state.in_(("running", "scheduled")))
            .values(state="paused", pause_reason=reason, version=email_campaigns.c.version + 1, updated_at=func.now())
            .returning(email_campaigns.c.id)
        )
    ).scalar_one_or_none()
    if paused is None:
        return None
    await session.execute(
        insert(campaign_schedule_index).values(campaign_id=campaign_id, tenant_id=tenant_id).on_conflict_do_nothing()
    )
    await audit(
        session, INGEST, "email_campaign.auto_paused", "email_campaign", campaign_id, tenant_id=tenant_id,
        reason=reason, sent=sent, bounced=counts.get("bounced", 0), complained=counts.get("complained", 0),
    )  # fmt: skip
    await emit(
        session,
        "notification.requested",
        tenant_id=tenant_id,
        actor=INGEST,
        payload={
            "template": "generic",
            "permission": "emails:campaigns:manage",
            "resource_type": "email_campaign",
            "resource_id": str(campaign_id),
            "link": f"{AUTO_PAUSE_LINK}/{campaign_id}",
            "channel": "in_app",
        },
    )
    log.warning("email_campaign_auto_paused", extra={"campaign_id": str(campaign_id), "reason": reason})
    return reason
