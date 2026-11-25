"""Outbox helpers: audit records, AI security events and approver notifications (ids and enums only)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.audit import emit_audit
from nexus_core.context import Actor, current_correlation_id
from nexus_core.messaging import enqueue
from nexus_events import ActorRef, make_event
from tool_execution.tables import outbox

PRODUCER = "tool-execution"
APPROVER_PERMISSION = "ai:approvals:decide"


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: Any,
    *,
    tenant_id: uuid.UUID | None,
    outcome: str = "success",
    **metadata: Any,
) -> None:
    await emit_audit(
        session,
        outbox,
        producer=PRODUCER,
        actor=actor,
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        metadata=metadata,
    )


def _cid() -> uuid.UUID:
    raw = current_correlation_id()
    try:
        return uuid.UUID(raw)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, raw)


def _ref(actor: Actor) -> ActorRef:
    return ActorRef(type=actor.type, id=actor.id[:128])  # type: ignore[arg-type]


async def security_event(
    session: AsyncSession,
    actor: Actor,
    tenant_id: uuid.UUID,
    kind: str,
    *,
    agent_id: uuid.UUID | None,
    tool_id: str | None,
    detail: str = "",
) -> None:
    await enqueue(
        session,
        outbox,
        make_event(
            "ai.security_event",
            producer=PRODUCER,
            actor=_ref(actor),
            correlation_id=_cid(),
            tenant_id=tenant_id,
            payload={
                "kind": kind,
                "agent_id": agent_id,
                "tool_id": (tool_id or "")[:80] or None,
                "detail": detail[:500],
            },
        ),
    )


async def notify_approvers(session: AsyncSession, actor: Actor, tenant_id: uuid.UUID, approval_id: uuid.UUID) -> None:
    """Everyone in the tenant holding `ai:approvals:decide` is told (notification-service fans out)."""
    await enqueue(
        session,
        outbox,
        make_event(
            "notification.requested",
            producer=PRODUCER,
            actor=_ref(actor),
            correlation_id=_cid(),
            tenant_id=tenant_id,
            payload={
                "template": "generic",
                "permission": APPROVER_PERMISSION,
                "resource_type": "ai_approval_request",
                "resource_id": str(approval_id),
                "link": f"/app/approvals/{approval_id}",
                "channel": "in_app",
            },
        ),
    )
