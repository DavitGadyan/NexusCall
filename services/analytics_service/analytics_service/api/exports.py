"""Analytics exports (`analytics:export`): an audited async job with a short-lived download link."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from analytics_service.exports import enqueue_export
from analytics_service.projections import METRICS
from analytics_service.reports import date_range
from analytics_service.schemas import ExportOut, ExportPage
from analytics_service.tables import exports, idempotency_keys, outbox
from nexus_core.audit import emit_audit
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.celery_app import JobContext
from nexus_core.context import current_correlation_id
from nexus_core.errors import NotFound, ServiceUnavailable, Unprocessable
from nexus_core.idempotency import IdempotencyStore
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.storage import ObjectStore, assert_tenant_key

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])
Export = Annotated[AuthContext, Depends(require_permission("analytics:export"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
PRODUCER = "analytics-service"
_idempotency = IdempotencyStore(idempotency_keys)
COLUMNS = (
    exports.c.id,
    exports.c.kind,
    exports.c.status,
    exports.c.params,
    exports.c.row_count,
    exports.c.error_code,
    exports.c.created_at,
    exports.c.completed_at,
    exports.c.object_key,
)


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    kind: Literal["daily_metrics"] = "daily_metrics"
    from_: date = Field(alias="from")
    to: date
    metrics: list[str] | None = Field(None, max_length=len(METRICS))
    include_dimensions: bool = False


def _out(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in ExportOut.model_fields if k in row}


def object_store(request: Request) -> ObjectStore:
    store: ObjectStore | None = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore.from_settings(request.app.state.settings)
        request.app.state.object_store = store
    return store


def celery_for(app: Any) -> Any:
    celery = getattr(app.state, "celery", None)
    if celery is None:
        from analytics_service.worker import celery_app

        celery = app.state.celery = celery_app
    return celery


@router.post("", status_code=202, response_model=ExportOut)
async def create_export(
    body: ExportIn,
    request: Request,
    ctx: Export,
    _: StepUp,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
) -> Any:
    tenant_id = ctx.require_tenant()
    settings = request.app.state.settings
    rng = date_range(body.from_, body.to, max_days=settings.max_range_days)
    if body.metrics and not set(body.metrics) <= METRICS:
        raise Unprocessable("Unknown metric in 'metrics'")
    params = {
        "from": rng.start.isoformat(),
        "to": rng.end.isoformat(),
        "metrics": sorted(set(body.metrics)) if body.metrics else None,
        "include_dimensions": body.include_dimensions,
    }
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        outcome = await _idempotency.begin(
            s,
            tenant_id=tenant_id,
            operation="analytics.export.create",
            key=idempotency_key,
            payload={"kind": body.kind, **params},
        )
        if not outcome.is_new:
            return JSONResponse(outcome.response_body, status_code=outcome.response_status or 202)
        row = (
            (
                await s.execute(
                    exports.insert()
                    .values(tenant_id=tenant_id, kind=body.kind, params=params, requested_by=ctx.principal.sub)
                    .returning(*COLUMNS)
                )
            )
            .mappings()
            .one()
        )
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=tenant_id,
            action="analytics.export.requested",
            resource_type="analytics_export",
            resource_id=row["id"],
            metadata={"kind": body.kind, **{k: v for k, v in params.items() if v is not None}},
        )
        response = ExportOut.model_validate(_out(row)).model_dump(mode="json")
        await _idempotency.succeed(s, outcome.record_id, status=202, body=response, resource_id=str(row["id"]))
    ctx_job = JobContext(
        tenant_id=tenant_id,
        correlation_id=_uuid(current_correlation_id()),
        actor_type=ctx.actor.type,
        actor_id=ctx.actor.id,
    )
    await enqueue_export(celery_for(request.app), ctx_job, row["id"])
    return JSONResponse(response, status_code=202)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        return uuid.uuid4()


@router.get("", response_model=ExportPage)
async def list_exports(request: Request, ctx: Export, limit: Limit = 50, cursor: Cursor = None) -> dict[str, Any]:
    stmt = apply_keyset(select(*COLUMNS), exports.c.created_at, exports.c.id, cursor, limit)
    async with request.app.state.db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(stmt)).mappings().all()
    page, next_cursor = build_page(list(rows), limit)
    return {"items": [_out(r) for r in page], "next_cursor": next_cursor}


@router.get("/{export_id}", response_model=ExportOut)
async def get_export(export_id: uuid.UUID, request: Request, ctx: Export) -> dict[str, Any]:
    """Returns a short presigned URL once the job succeeded. Every link issued is audited."""
    tenant_id = ctx.require_tenant()
    seconds = int(request.app.state.settings.export_url_seconds)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        row = (await s.execute(select(*COLUMNS).where(exports.c.id == export_id))).mappings().first()
        if row is None:
            raise NotFound("Export not found")
        out = _out(row)
        if row["status"] != "succeeded" or not row["object_key"]:
            return out
        key = assert_tenant_key(tenant_id, row["object_key"])
        await emit_audit(
            s,
            outbox,
            producer=PRODUCER,
            actor=ctx.actor,
            tenant_id=tenant_id,
            action="analytics.export.downloaded",
            resource_type="analytics_export",
            resource_id=export_id,
            metadata={"rows": row["row_count"], "expires_in_seconds": seconds},
        )
    try:
        url = await object_store(request).presign_get(
            "exports", key, expires_seconds=seconds, download_filename=f"analytics-{export_id}.csv"
        )
    except Exception as exc:
        raise ServiceUnavailable("Export storage is unavailable") from exc
    return {**out, "download_url": url, "expires_in_seconds": seconds}
