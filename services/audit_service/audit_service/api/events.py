"""Tenant audit log (Tenant Admin Console, build.md §18.1B): search and audited CSV export."""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from audit_service.api.common import (
    LIST_COLUMNS,
    AuditEventOut,
    AuditFilters,
    AuditPage,
    FilterBody,
    audit_filters,
    db,
    object_store,
    record_self,
)
from audit_service.tables import audit_events
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import ServiceUnavailable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.storage import tenant_key

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
ReadAudit = Annotated[AuthContext, Depends(require_permission("audit:read"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
Filters = Annotated[AuditFilters, Depends(audit_filters)]

CSV_COLUMNS = (
    "id",
    "occurred_at",
    "actor_type",
    "actor_id",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "request_id",
    "correlation_id",
    "producer",
    "metadata",
)


def _item(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in AuditEventOut.model_fields}


@router.get("/events", response_model=AuditPage)
async def list_events(
    request: Request, ctx: ReadAudit, filters: Filters, limit: Limit = 50, cursor: Cursor = None
) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    stmt = filters.apply(select(*LIST_COLUMNS))
    # RLS limits rows to this tenant; the explicit predicate keeps the index usable and is defense in depth.
    stmt = apply_keyset(
        stmt.where(audit_events.c.tenant_id == tenant_id),
        audit_events.c.occurred_at,
        audit_events.c.id,
        cursor,
        limit,
    )
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        rows = (await s.execute(stmt)).mappings().all()
    page, next_cursor = build_page(list(rows), limit, created_attr="occurred_at")
    return {"items": [_item(r) for r in page], "next_cursor": next_cursor}


class ExportOut(BaseModel):
    export_id: uuid.UUID
    rows: int
    truncated: bool
    download_url: str
    expires_in_seconds: int


def _cell(value: Any) -> str:
    """CSV cell without spreadsheet formula injection (cells starting with = + - @ tab CR are quoted)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = value.astimezone(UTC).isoformat()
    elif isinstance(value, dict):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def render_csv(rows: list[Any]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_cell(row[c]) for c in CSV_COLUMNS])
    return buf.getvalue().encode()


@router.post("/exports", response_model=ExportOut, status_code=201)
async def export_events(body: FilterBody, request: Request, ctx: ReadAudit, _: StepUp) -> dict[str, Any]:
    """Bounded, synchronous CSV export to the private `exports` bucket; returns a short presigned URL."""
    tenant_id = ctx.require_tenant()
    settings = request.app.state.settings
    filters = body.filters()
    max_rows = int(settings.audit_export_max_rows)
    stmt = (
        filters.apply(select(*LIST_COLUMNS))
        .where(audit_events.c.tenant_id == tenant_id)
        .order_by(audit_events.c.occurred_at.desc(), audit_events.c.id.desc())
        .limit(max_rows + 1)
    )
    database = db(request)
    async with database.tenant_session(tenant_id, ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    export_id = uuid.uuid4()
    key = tenant_key(tenant_id, "audit", f"{export_id}.csv")
    store = object_store(request)
    meta = {**filters.describe(), "rows": len(rows), "truncated": truncated}
    try:
        await store.put("exports", key, render_csv(rows), content_type="text/csv")
    except Exception as exc:
        await record_self(
            database,
            actor=ctx.actor,
            tenant_id=tenant_id,
            action="audit.export.created",
            resource_type="audit_export",
            resource_id=str(export_id),
            outcome="failure",
            metadata={**meta, "error": type(exc).__name__},
        )
        raise ServiceUnavailable("Export storage is unavailable; try again later") from exc
    await record_self(
        database,
        actor=ctx.actor,
        tenant_id=tenant_id,
        action="audit.export.created",
        resource_type="audit_export",
        resource_id=str(export_id),
        metadata=meta,
    )
    seconds = int(settings.audit_export_url_seconds)
    url = await store.presign_get("exports", key, expires_seconds=seconds, download_filename=f"audit-{export_id}.csv")
    return {
        "export_id": export_id,
        "rows": len(rows),
        "truncated": truncated,
        "download_url": url,
        "expires_in_seconds": seconds,
    }
