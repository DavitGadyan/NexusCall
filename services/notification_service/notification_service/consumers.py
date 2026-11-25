"""Domain events -> in-app notifications (platform-services brief, notification-service consumers).

Titles and bodies are code-defined text with identifiers and counts only: no content from the event
producer's free text ever reaches a notification. Each notification is deduped on the event id, so a
redelivered (or directly re-invoked) event creates nothing the second time.

`notification.requested` with `channel=email` cannot be delivered by email yet: resolving a user's or a
role's address needs a tenant-service directory endpoint that does not exist. The audience gets the
in-app notification instead (see the integration request in the report).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.messaging import PermanentEventError
from nexus_events import EventEnvelope, payload_model_for
from notification_service.domain import create_notification, publish_hint

log = logging.getLogger(__name__)
Handler = Callable[[EventEnvelope, AsyncSession], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class InApp:
    kind: str
    title: str
    body: str
    link: str | None
    permission: str | None = None
    user_id: uuid.UUID | None = None


def _usd(micros: int) -> str:
    return f"{micros / 1_000_000:.2f}"


def _draft_ready(p: Any) -> InApp:
    return InApp(
        "draft_ready",
        "AI reply draft ready for review",
        "A new AI-generated reply draft is waiting for review. Nothing is sent until it is approved.",
        f"/inbox/threads/{p.thread_id}?draft={p.draft_id}",
        permission="emails:drafts:review",
    )


def _handoff(p: Any) -> InApp:
    return InApp(
        "handoff_requested",
        "A caller asked for a human",
        "A live call needs a human agent.",
        f"/calls/{p.call_id}",
        permission="calls:manage",
    )


def _webhook_failed(p: Any) -> InApp:
    return InApp(
        "webhook_failed",
        "Webhook delivery failed",
        f"A webhook delivery failed after {p.attempts} attempts and will not be retried automatically.",
        f"/integrations/webhooks/{p.endpoint_id}",
        permission="integrations:manage",
    )


def _budget(p: Any) -> InApp:
    return InApp(
        "ai_budget",
        "AI budget exceeded",
        f"The {p.scope} AI budget for {p.period} is used up ({_usd(p.used_micros)} of {_usd(p.limit_micros)} USD). "
        "AI requests are paused until the budget is raised or the period ends.",
        "/admin/ai",
        permission="tenant:manage",
    )


def _import_done(p: Any) -> InApp:
    return InApp(
        "import_completed",
        "Lead import finished",
        f"Imported {p.total} rows: {p.created} created, {p.updated} updated, {p.failed} failed.",
        f"/crm/imports/{p.import_id}",
        permission="leads:create",
    )


# notification.requested templates -> in-app text and the default audience when the payload names none.
REQUESTED_TEMPLATES: dict[str, tuple[str, str, str | None]] = {
    "support_access_requested": (
        "Support access requested",
        "The platform team requested time-limited, read-only support access to this workspace.",
        "tenant:manage",
    ),
    "support_access_active": (
        "Support access granted",
        "Time-limited, read-only support access to this workspace is active. Every read is audited.",
        "tenant:manage",
    ),
    "support_access_rejected": (
        "Support access rejected",
        "A support access request for this workspace was rejected. No access was granted.",
        "tenant:manage",
    ),
    "draft_ready": (
        "AI reply draft ready for review",
        "An AI reply draft is waiting for review.",
        "emails:drafts:review",
    ),
    "handoff_requested": ("A caller asked for a human", "A live call needs a human agent.", "calls:manage"),
    "ai_approval_requested": (
        "AI action needs approval",
        "An AI agent proposed an action that needs a human decision before it runs.",
        "ai:approvals:decide",
    ),
    "booking_reminder": ("Upcoming booking", "A booking is coming up soon.", None),
    "credential_rotation_due": (
        "Credential rotation due",
        "A provider credential is due for rotation.",
        "integrations:manage",
    ),
    "generic": ("Notification", "There is an update in your workspace.", None),
}


def _requested(p: Any) -> InApp | None:
    title, body, default_permission = REQUESTED_TEMPLATES.get(p.template, REQUESTED_TEMPLATES["generic"])
    kind = p.template if p.template in REQUESTED_TEMPLATES else "generic"
    if p.recipient_user_id is not None:
        return InApp(kind, title, body, p.link, user_id=p.recipient_user_id)
    permission = p.permission or default_permission
    if permission is None:
        return None
    return InApp(kind, title, body, p.link, permission=permission)


BUILDERS: dict[str, Callable[[Any], InApp | None]] = {
    "notification.requested": _requested,
    "email.reply_draft.generated": _draft_ready,
    "voice.handoff.requested": _handoff,
    "webhook.delivery.failed": _webhook_failed,
    "ai.budget.exceeded": _budget,
    "import.completed": _import_done,
}

_ANY_SERVICE = {
    "tenant-service",
    "crm-service",
    "campaign-service",
    "call-service",
    "voice-service",
    "ai-agent-service",
    "ai-gateway",
    "mcp-registry",
    "tool-execution",
    "booking-service",
    "email-service",
    "email-inbox-service",
    "integration-service",
    "analytics-service",
    "audit-service",
}
ALLOWED_PRODUCERS: dict[str, set[str]] = {
    "notification.requested": _ANY_SERVICE,
    "email.reply_draft.generated": {"email-inbox-service", "ai-agent-service"},
    "voice.handoff.requested": {"voice-service"},
    "webhook.delivery.failed": {"integration-service"},
    "ai.budget.exceeded": {"ai-gateway"},
    "import.completed": {"crm-service"},
}


def _payload(event: EventEnvelope) -> BaseModel:
    model = payload_model_for(event.event_type, event.event_version)
    if model is None:
        raise PermanentEventError(f"unsupported event version {event.event_type}.v{event.event_version}")
    try:
        return model.model_validate(event.payload)
    except ValidationError as exc:
        raise PermanentEventError(f"invalid {event.event_type} payload") from exc


def make_handlers(redis_source: Callable[[], Any]) -> dict[str, Handler]:
    """`redis_source` returns the app's Redis client (or None) at call time, for live hints."""

    async def handle(event: EventEnvelope, session: AsyncSession) -> None:
        if event.tenant_id is None:
            log.warning("notification_event_without_tenant", extra={"event_type": event.event_type})
            return
        spec = BUILDERS[event.event_type](_payload(event))
        if spec is None:
            log.info("notification_without_audience", extra={"event_type": event.event_type})
            return
        try:
            nid = await create_notification(
                session,
                tenant_id=event.tenant_id,
                user_id=spec.user_id,
                permission=spec.permission,
                kind=spec.kind,
                title=spec.title,
                body=spec.body,
                link=spec.link,
                source=event.producer,
                dedupe_key=f"event:{event.event_id}",
            )
        except ValueError as exc:
            raise PermanentEventError(str(exc)) from exc
        if nid is None:
            log.info("notification_duplicate_event", extra={"event_id": str(event.event_id)})
            return
        # Best effort; published before commit, so a client may refetch a moment early (the bell refetches).
        await publish_hint(
            redis_source(), event.tenant_id, nid, kind=spec.kind, user_id=spec.user_id, permission=spec.permission
        )

    return {event_type: handle for event_type in BUILDERS}
