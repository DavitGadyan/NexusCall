"""Analytics export jobs (BUILD_CONVENTIONS §3 "Async work").

1. The API writes the `exports` row as `queued` with its audit event and idempotency record in one
   transaction, then enqueues `analytics.build_export` on the `analytics.aggregate` queue after commit.
2. `build_export` claims the row atomically (`queued -> processing`), so duplicate deliveries do nothing,
   builds the CSV from the tenant's `daily_metrics` inside an RLS session, and writes it to the private
   `exports` bucket under the tenant's key prefix.
3. A periodic sweeper re-enqueues rows stuck in `queued` and returns stale `processing` rows to `queued`.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from analytics_service.tables import daily_metrics, exports, outbox
from nexus_core.audit import emit_audit
from nexus_core.celery_app import JobContext, TransientFailure, enqueue_task
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.storage import ObjectStore, tenant_key

log = logging.getLogger(__name__)
PRODUCER = "analytics-service"
TASK_NAME = "analytics.build_export"
QUEUE = "analytics.aggregate"
WORKER_ACTOR = Actor("service", "analytics-worker")
CSV_COLUMNS = ("day", "metric", "dimension", "value")


def export_key(tenant_id: uuid.UUID, export_id: uuid.UUID) -> str:
    return tenant_key(tenant_id, "analytics", f"{export_id}.csv")


def _cell(value: Any) -> str:
    text_value = "" if value is None else str(value)
    return f"'{text_value}" if text_value[:1] in ("=", "+", "-", "@", "\t", "\r") else text_value


def render_csv(rows: list[Any]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_cell(row[c]) for c in CSV_COLUMNS])
    return buf.getvalue().encode()


async def build_export(
    db: Database, store: ObjectStore, ctx: JobContext, export_id: uuid.UUID, *, max_rows: int, max_attempts: int
) -> str:
    """Task body (plain coroutine; tests call it directly). Returns the resulting status."""
    tenant_id = ctx.tenant_id
    async with db.tenant_session(tenant_id, WORKER_ACTOR) as s:
        claimed = (
            (
                await s.execute(
                    update(exports)
                    .where(exports.c.id == export_id, exports.c.status == "queued")
                    .values(status="processing", attempts=exports.c.attempts + 1, updated_at=func.now())
                    .returning(exports.c.params, exports.c.attempts)
                )
            )
            .mappings()
            .first()
        )
        if claimed is None:
            return "skipped"  # unknown, other tenant, or already claimed by a duplicate delivery
        params, attempts = claimed["params"], int(claimed["attempts"])
        stmt = select(
            daily_metrics.c.day, daily_metrics.c.metric, daily_metrics.c.dimension, daily_metrics.c.value
        ).where(
            daily_metrics.c.day >= date.fromisoformat(params["from"]),
            daily_metrics.c.day <= date.fromisoformat(params["to"]),
        )
        if params.get("metrics"):
            stmt = stmt.where(daily_metrics.c.metric.in_(params["metrics"]))
        if not params.get("include_dimensions", False):
            stmt = stmt.where(daily_metrics.c.dimension == "")
        rows = (
            (
                await s.execute(
                    stmt.order_by(daily_metrics.c.day, daily_metrics.c.metric, daily_metrics.c.dimension).limit(
                        max_rows
                    )
                )
            )
            .mappings()
            .all()
        )
    key = export_key(tenant_id, export_id)
    try:
        await store.put("exports", key, render_csv(list(rows)), content_type="text/csv")
    except Exception as exc:
        final = attempts >= max_attempts
        async with db.tenant_session(tenant_id, WORKER_ACTOR) as s:
            await s.execute(
                update(exports)
                .where(exports.c.id == export_id, exports.c.status == "processing")
                .values(
                    status="failed" if final else "queued",
                    error_code="storage_unavailable" if final else None,
                    updated_at=func.now(),
                )
            )
            if final:
                await emit_audit(
                    s, outbox, producer=PRODUCER, actor=WORKER_ACTOR, tenant_id=tenant_id,
                    action="analytics.export.failed", resource_type="analytics_export", resource_id=export_id,
                    outcome="failure", metadata={"error": "storage_unavailable", "attempts": attempts},
                )  # fmt: skip
        if final:
            log.error("analytics_export_failed", extra={"export_id": str(export_id)})
            return "failed"
        raise TransientFailure("export storage unavailable") from exc
    async with db.tenant_session(tenant_id, WORKER_ACTOR) as s:
        await s.execute(
            update(exports)
            .where(exports.c.id == export_id, exports.c.status == "processing")
            .values(
                status="succeeded",
                object_key=key,
                row_count=len(rows),
                error_code=None,
                completed_at=func.now(),
                updated_at=func.now(),
            )
        )
        await emit_audit(
            s, outbox, producer=PRODUCER, actor=WORKER_ACTOR, tenant_id=tenant_id,
            action="analytics.export.completed", resource_type="analytics_export", resource_id=export_id,
            metadata={"rows": len(rows), "truncated": len(rows) >= max_rows},
        )  # fmt: skip
    return "succeeded"


async def enqueue_export(celery: Any, ctx: JobContext, export_id: uuid.UUID) -> bool:
    """Publish after commit. A failure is logged only: the sweeper re-enqueues queued rows."""
    try:
        await enqueue_task(celery, TASK_NAME, QUEUE, ctx, export_id=str(export_id))
    except Exception:  # noqa: BLE001 - the row stays `queued` and the sweeper retries
        log.warning("analytics_export_enqueue_failed", extra={"export_id": str(export_id)})
        return False
    return True


async def sweep_exports(
    app_db: Database, platform_db: Database, celery: Any, *, stale_queued_seconds: int, stale_processing_seconds: int
) -> int:
    """Find stuck jobs across tenants through the read-only platform role, then act per tenant in RLS."""
    now = datetime.now(UTC)
    queued_cutoff = now - timedelta(seconds=stale_queued_seconds)
    processing_cutoff = now - timedelta(seconds=stale_processing_seconds)
    async with platform_db.global_session(Actor("system", "export-sweeper")) as s:
        stuck = (
            (
                await s.execute(
                    select(exports.c.id, exports.c.tenant_id, exports.c.status)
                    .where(
                        or_(
                            and_(exports.c.status == "queued", exports.c.updated_at < queued_cutoff),
                            and_(exports.c.status == "processing", exports.c.updated_at < processing_cutoff),
                        )
                    )
                    .order_by(exports.c.updated_at)
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
    count = 0
    for row in stuck:
        async with app_db.tenant_session(row["tenant_id"], Actor("system", "export-sweeper")) as s:
            await s.execute(
                update(exports)
                .where(exports.c.id == row["id"], exports.c.status.in_(("queued", "processing")))
                .values(status="queued", updated_at=func.now())
            )
        ctx = JobContext(tenant_id=row["tenant_id"], correlation_id=uuid.uuid4(), actor_type="system",
                         actor_id="export-sweeper")  # fmt: skip
        if await enqueue_export(celery, ctx, row["id"]):
            count += 1
    return count
