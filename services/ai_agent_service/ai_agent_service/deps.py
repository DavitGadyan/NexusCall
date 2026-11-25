"""Shared helpers: audit, domain events, background job index and enqueueing."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_agent_service.settings import INGEST_QUEUE
from ai_agent_service.tables import agent_jobs, outbox
from nexus_core.audit import emit_audit
from nexus_core.celery_app import JobContext
from nexus_core.context import Actor, current_correlation_id
from nexus_core.messaging import enqueue
from nexus_events import ActorRef, make_event

PRODUCER = "ai-agent-service"
log = logging.getLogger(__name__)

TASKS = {
    "ingest": ("ai.knowledge_ingest", "document_id"),
    "reembed": ("ai.knowledge_reembed", "job_id"),
    "eval": ("ai.eval_run", "run_id"),
}


async def audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: Any = None,
    *,
    tenant_id: uuid.UUID,
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


async def emit(
    session: AsyncSession, event_type: str, *, tenant_id: uuid.UUID, actor: Actor, payload: dict[str, Any]
) -> None:
    """Domain event through the transactional outbox (ids and enums only)."""
    event = make_event(
        event_type,
        producer=PRODUCER,
        actor=ActorRef(type=actor.type, id=actor.id[:128]),  # type: ignore[arg-type]
        correlation_id=current_correlation_id(),
        tenant_id=tenant_id,
        payload=payload,
    )
    await enqueue(session, outbox, event)


def job_context(tenant_id: uuid.UUID, actor: Actor) -> JobContext:
    return JobContext(
        tenant_id=tenant_id,
        correlation_id=uuid.UUID(current_correlation_id()),
        actor_type=actor.type,
        actor_id=actor.id[:128],
    )


async def register_job(session: AsyncSession, job_id: uuid.UUID, tenant_id: uuid.UUID, kind: str) -> None:
    """IDs-only job index row, written in the same transaction as the business row."""
    await session.execute(
        insert(agent_jobs)
        .values(job_id=job_id, tenant_id=tenant_id, kind=kind, status="queued")
        .on_conflict_do_update(index_elements=["job_id"], set_={"status": "queued"})
    )


async def finish_job(session: AsyncSession, job_id: uuid.UUID) -> None:
    await session.execute(update(agent_jobs).where(agent_jobs.c.job_id == job_id).values(status="done"))


async def publish(app: Any, kind: str, ctx: JobContext, job_id: uuid.UUID) -> None:
    """Publish after commit. A failure is logged only: the sweeper re-enqueues rows still `queued`."""
    name, arg = TASKS[kind]
    try:
        await app.state.enqueue(name, INGEST_QUEUE, ctx, **{arg: str(job_id)})
    except Exception:  # noqa: BLE001 - the sweeper recovers; never fail the committed request
        log.warning("task_enqueue_failed", extra={"task": name})
