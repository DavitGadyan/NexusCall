"""Suppression list (do-not-call / do-not-email). Removal needs step-up and is audited."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from crm_service.deps import audit, db
from crm_service.domain import add_suppression
from crm_service.normalize import mask_email, mask_phone
from crm_service.schemas import In, Items
from crm_service.tables import suppressions
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import NotFound
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/suppressions", tags=["suppressions"])
Update = Annotated[AuthContext, Depends(require_permission("leads:update"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]


class SuppressionIn(In):
    channel: Literal["phone", "email", "all"]
    contact_id: uuid.UUID | None = None
    value: str | None = Field(default=None, min_length=3, max_length=320)
    reason: Literal["opt_out", "bounce", "complaint", "manual", "legal"] = "manual"


class SuppressionOut(BaseModel):
    id: uuid.UUID
    channel: str
    value_masked: str | None
    contact_id: uuid.UUID | None
    reason: str
    source_type: str
    created_by: str
    created_at: datetime


def _mask(row: dict) -> str | None:  # type: ignore[type-arg]
    value = row["value"]
    if value is None:
        return None
    return mask_email(value) if "@" in value else mask_phone(value)


def _out(row: dict) -> SuppressionOut:  # type: ignore[type-arg]
    return SuppressionOut(
        **{k: row[k] for k in SuppressionOut.model_fields if k != "value_masked"}, value_masked=_mask(row)
    )


@router.get("", response_model=Items[SuppressionOut])
async def list_suppressions(
    request: Request,
    ctx: Update,
    limit: Limit = 50,
    cursor: Cursor = None,
    channel: Literal["phone", "email", "all"] | None = None,
    contact_id: uuid.UUID | None = None,
) -> Items[SuppressionOut]:
    stmt = select(suppressions)
    if channel:
        stmt = stmt.where(suppressions.c.channel == channel)
    if contact_id:
        stmt = stmt.where(suppressions.c.contact_id == contact_id)
    stmt = apply_keyset(stmt, suppressions.c.created_at, suppressions.c.id, cursor, limit)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    return Items[SuppressionOut](items=[_out(r) for r in page], next_cursor=next_cursor)


@router.post("", status_code=201, response_model=SuppressionOut)
async def create_suppression(body: SuppressionIn, request: Request, ctx: Update) -> SuppressionOut:
    """Idempotent by nature: adding an existing suppression returns it."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        suppression_id, _ = await add_suppression(
            s,
            tenant_id=tenant_id,
            channel=body.channel,
            reason=body.reason,
            source_type="user",
            source_id=ctx.actor.id,
            actor=ctx.actor,
            contact_id=body.contact_id,
            value=body.value,
        )
        row = (await s.execute(select(suppressions).where(suppressions.c.id == suppression_id))).mappings().one()
    return _out(dict(row))


@router.delete("/{suppression_id}", status_code=204)
async def remove_suppression(suppression_id: uuid.UUID, request: Request, ctx: Update, _: StepUp) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (
            await s.execute(
                delete(suppressions)
                .where(suppressions.c.id == suppression_id)
                .returning(suppressions.c.channel, suppressions.c.reason, suppressions.c.contact_id)
            )
        ).first()
        if row is None:
            raise NotFound("Suppression not found")
        await audit(
            s,
            ctx.actor,
            "suppression.removed",
            "suppression",
            suppression_id,
            tenant_id=tenant_id,
            channel=row.channel,
            reason=row.reason,
            contact_id=str(row.contact_id) if row.contact_id else None,
        )
    return Response(status_code=204)
