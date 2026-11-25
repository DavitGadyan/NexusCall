"""`audit.event` consumer: every producer's audit records land in the tenant's (or platform) chain.

The EventConsumer has already opened the RLS session for the envelope's tenant (or a session without
tenant context for platform events) and claimed the inbox row, so a redelivery is a no-op. `append`
additionally dedupes on `event_id`, which keeps direct/duplicate invocations idempotent too."""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from audit_service.chain import AuditRecord, append
from nexus_core.messaging import PermanentEventError
from nexus_events import EventEnvelope
from nexus_events.payloads import AuditEventPayload

log = logging.getLogger(__name__)


async def handle_audit_event(event: EventEnvelope, session: AsyncSession) -> None:
    try:
        payload = AuditEventPayload.model_validate(event.payload)
    except ValidationError as exc:
        log.error("audit_event_invalid", extra={"event_id": str(event.event_id), "producer": event.producer})
        raise PermanentEventError("invalid audit.event payload") from exc
    row_id = await append(
        session,
        AuditRecord(
            event_id=event.event_id,
            tenant_id=event.tenant_id,
            occurred_at=event.occurred_at,
            actor_type=event.actor.type,
            actor_id=event.actor.id,
            action=payload.action,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            outcome=payload.outcome,
            request_id=payload.request_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            producer=event.producer,
            metadata=payload.metadata,
        ),
    )
    if row_id is None:
        log.info("audit_event_duplicate", extra={"event_id": str(event.event_id)})


EVENT_HANDLERS = {"audit.event": handle_audit_event}
