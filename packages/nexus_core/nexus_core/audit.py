"""Audit event emission through the outbox (build.md §3.1P).

Audit records travel transactionally with the business change, so a committed
action can never lose its audit record. Metadata is filtered to safe keys.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.context import Actor, current_correlation_id, request_id_var
from nexus_core.logging import SENSITIVE_KEYS, redact
from nexus_core.messaging import enqueue
from nexus_events import ActorRef, make_event


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    return {k: redact(v, k) for k, v in metadata.items() if k.lower() not in SENSITIVE_KEYS}


async def emit_audit(
    session: AsyncSession,
    outbox: Table,
    *,
    producer: str,
    actor: Actor,
    tenant_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID | None = None,
    outcome: str = "success",
    metadata: dict[str, Any] | None = None,
) -> None:
    event = make_event(
        "audit.event",
        producer=producer,
        actor=ActorRef(type=actor.type, id=actor.id),  # type: ignore[arg-type]
        correlation_id=current_correlation_id(),
        tenant_id=tenant_id,
        payload={
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id) if resource_id else None,
            "outcome": outcome,
            "request_id": request_id_var.get(),
            "metadata": _safe_metadata(metadata),
        },
    )
    await enqueue(session, outbox, event)
