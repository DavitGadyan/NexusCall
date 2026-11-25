"""Lead exports: step-up, audited, asynchronous (CSV in the `exports` bucket, short presigned download)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from crm_service import idem
from crm_service.deps import audit, db, enqueue_job, job_context, store
from crm_service.schemas import In, Items, LeadStatus
from crm_service.tables import crm_jobs, exports
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import NotFound
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.storage import assert_tenant_key

router = APIRouter(prefix="/api/v1/leads/exports", tags=["exports"])
Export = Annotated[AuthContext, Depends(require_permission("leads:export"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
TASK = "exports.build"


class ExportFilters(In):
    status: list[LeadStatus] | None = Field(default=None, max_length=7)
    source: str | None = Field(default=None, max_length=64)
    campaign_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


class ExportIn(In):
    kind: Literal["leads"] = "leads"
    filters: ExportFilters = Field(default_factory=ExportFilters)


class ExportOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    row_count: int | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    download_url: str | None = None
    expires_in: int | None = None


def to_out(row: Any, url: str | None = None, expires: int | None = None) -> ExportOut:
    return ExportOut(
        **{k: row[k] for k in ExportOut.model_fields if k not in ("download_url", "expires_in")},
        download_url=url,
        expires_in=expires,
    )


@router.post("", status_code=202, response_model=ExportOut)
async def request_export(
    body: ExportIn,
    request: Request,
    ctx: Export,
    _: StepUp,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    tenant_id = ctx.require_tenant()
    export_id = uuid.uuid4()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem.begin(
            s,
            tenant_id=tenant_id,
            operation="leads.export",
            key=idem.require_key(idempotency_key),
            payload=body.model_dump(mode="json"),
        )
        if op.replay is not None:
            return op.replay
        row = (
            (
                await s.execute(
                    insert(exports)
                    .values(
                        id=export_id,
                        tenant_id=tenant_id,
                        kind=body.kind,
                        filters=body.filters.model_dump(mode="json", exclude_none=True),
                        created_by=str(ctx.actor),
                    )
                    .returning(exports)
                )
            )
            .mappings()
            .one()
        )
        await s.execute(insert(crm_jobs).values(job_id=export_id, tenant_id=tenant_id, kind="export", status="queued"))
        await audit(
            s,
            ctx.actor,
            "leads.export.requested",
            "export",
            export_id,
            tenant_id=tenant_id,
            filters=sorted(body.filters.model_dump(exclude_none=True)),
        )
        result = await op.done(202, to_out(row).model_dump(mode="json"), export_id)
    await enqueue_job(request, TASK, job_context(tenant_id, ctx.actor), export_id=str(export_id))
    return result


@router.get("", response_model=Items[ExportOut])
async def list_exports(request: Request, ctx: Export, limit: Limit = 50, cursor: Cursor = None) -> Items[ExportOut]:
    stmt = apply_keyset(select(exports), exports.c.created_at, exports.c.id, cursor, limit)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
    page, next_cursor = build_page(rows, limit)
    return Items[ExportOut](items=[to_out(r) for r in page], next_cursor=next_cursor)


@router.get("/{export_id}", response_model=ExportOut)
async def get_export(export_id: uuid.UUID, request: Request, ctx: Export) -> ExportOut:
    """Returns a short-lived download URL once the file is ready; every URL issued is audited."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (await s.execute(select(exports).where(exports.c.id == export_id))).mappings().first()
        if row is None:
            raise NotFound("Export not found")
        if row["status"] != "completed" or row["object_key"] is None:
            return to_out(row)
        seconds = int(request.app.state.settings.presign_seconds)
        url = await store(request).presign_get(
            "exports",
            assert_tenant_key(tenant_id, row["object_key"]),
            expires_seconds=seconds,
            download_filename=f"leads-{export_id}.csv",
        )
        await audit(s, ctx.actor, "leads.export.downloaded", "export", export_id, tenant_id=tenant_id)
    return to_out(row, url, seconds)
