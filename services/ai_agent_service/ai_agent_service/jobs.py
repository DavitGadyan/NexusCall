"""Stuck-job sweeper (BUILD_CONVENTIONS §3 async pattern). Runs as a Redis-locked periodic job."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from ai_agent_service.deps import TASKS
from ai_agent_service.gateway import Deps
from ai_agent_service.knowledge import purge_object
from ai_agent_service.settings import INGEST_QUEUE
from ai_agent_service.tables import agent_jobs
from nexus_core.celery_app import JobContext

log = logging.getLogger(__name__)
Enqueue = Callable[..., Awaitable[None]]


async def sweep_jobs(deps: Deps, enqueue: Enqueue, *, stale_seconds: int, max_attempts: int, batch: int = 100) -> int:
    """Re-enqueue jobs left queued (lost publish, crashed worker) and retry pending object deletions."""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_seconds)
    j = agent_jobs.c
    async with deps.db.global_session() as session:
        rows = (
            await session.execute(
                update(agent_jobs)
                .where(
                    j.job_id.in_(
                        select(j.job_id)
                        .where(j.status != "done", j.updated_at < cutoff)
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                )
                .values(updated_at=func.now(), attempts=j.attempts + 1)
                .returning(j.job_id, j.tenant_id, j.kind, j.attempts)
            )
        ).all()
        abandoned = [r.job_id for r in rows if r.attempts > max_attempts]
        if abandoned:
            await session.execute(update(agent_jobs).where(j.job_id.in_(abandoned)).values(status="done"))
            log.error("jobs_abandoned", extra={"count": len(abandoned)})
    for r in rows:
        if r.job_id in abandoned:
            continue
        if r.kind == "object_delete":
            await purge_object(deps, r.tenant_id, r.job_id)
            continue
        name, arg = TASKS[r.kind]
        ctx = JobContext(
            tenant_id=r.tenant_id, correlation_id=uuid.uuid4(), actor_type="system", actor_id="ai-agent-sweeper"
        )
        try:
            await enqueue(name, INGEST_QUEUE, ctx, **{arg: str(r.job_id)})
        except Exception:  # noqa: BLE001 - tried again on the next sweep
            log.warning("sweeper_enqueue_failed", extra={"job_id": str(r.job_id)})
    return len(rows)
