"""Inbound number management (calls:manage, audited). ADR-0003: numbers are entered manually (Twilio number SID).

The ids-only `number_directory` (E.164 -> tenant) is written in the same transaction, so inbound webhooks can
resolve the tenant from trusted configuration only.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from call_service.deps import AppDeps
from call_service.domain import audit
from call_service.schemas import InboundNumberIn, InboundNumberOut, InboundNumberPage, InboundNumberPatch
from call_service.tables import inbound_numbers, number_directory
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/inbound-numbers", tags=["inbound-numbers"])
Manage = Annotated[AuthContext, Depends(require_permission("calls:manage"))]


def _out(row: Any) -> InboundNumberOut:
    return InboundNumberOut(**{k: row[k] for k in InboundNumberOut.model_fields})


@router.get("", response_model=InboundNumberPage)
async def list_numbers(request: Request, ctx: Manage, limit: Limit = 50, cursor: Cursor = None) -> InboundNumberPage:
    async with AppDeps(request.app).db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = apply_keyset(select(inbound_numbers), inbound_numbers.c.created_at, inbound_numbers.c.id, cursor, limit)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings()]
    page, next_cursor = build_page(rows, limit)
    return InboundNumberPage(items=[_out(r) for r in page], next_cursor=next_cursor)


@router.post("", status_code=201, response_model=InboundNumberOut)
async def create_number(body: InboundNumberIn, request: Request, ctx: Manage) -> InboundNumberOut:
    tenant_id = ctx.require_tenant()
    async with AppDeps(request.app).db.tenant_session(tenant_id, ctx.actor) as s:
        # The directory is global: a number already owned by ANY tenant conflicts (without saying which).
        claimed = (
            await s.execute(
                insert(number_directory)
                .values(e164=body.e164, tenant_id=tenant_id, inbound_number_id=uuid.uuid4(), status="active")
                .on_conflict_do_nothing()
                .returning(number_directory.c.inbound_number_id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            raise Conflict("This phone number is already registered")
        row = (
            (
                await s.execute(
                    insert(inbound_numbers)
                    .values(id=claimed, tenant_id=tenant_id, **body.model_dump())
                    .returning(inbound_numbers)
                )
            )
            .mappings()
            .one()
        )
        await audit(s, ctx.actor, "inbound_number.created", "inbound_number", row["id"], tenant_id=tenant_id)
    return _out(row)


@router.get("/{number_id}", response_model=InboundNumberOut)
async def get_number(number_id: uuid.UUID, request: Request, ctx: Manage) -> InboundNumberOut:
    async with AppDeps(request.app).db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = (await s.execute(select(inbound_numbers).where(inbound_numbers.c.id == number_id))).mappings().first()
    if row is None:
        raise NotFound("Inbound number not found")
    return _out(row)


@router.patch("/{number_id}", response_model=InboundNumberOut)
async def patch_number(
    number_id: uuid.UUID, body: InboundNumberPatch, request: Request, ctx: Manage
) -> InboundNumberOut:
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    async with AppDeps(request.app).db.tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (
                await s.execute(
                    update(inbound_numbers)
                    .where(inbound_numbers.c.id == number_id, inbound_numbers.c.version == body.expected_version)
                    .values(**changes, version=inbound_numbers.c.version + 1, updated_at=func.now())
                    .returning(inbound_numbers)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            exists = (
                await s.execute(select(inbound_numbers.c.id).where(inbound_numbers.c.id == number_id))
            ).scalar_one_or_none()
            if exists is None:
                raise NotFound("Inbound number not found")
            raise Conflict("Inbound number was changed by someone else; reload and retry")
        if "status" in changes:
            await s.execute(
                update(number_directory)
                .where(number_directory.c.inbound_number_id == number_id, number_directory.c.tenant_id == tenant_id)
                .values(status=row["status"], updated_at=func.now())
            )
        await audit(
            s,
            ctx.actor,
            "inbound_number.updated",
            "inbound_number",
            number_id,
            tenant_id=tenant_id,
            fields=sorted(changes),
        )
    return _out(row)


@router.delete("/{number_id}", status_code=204)
async def delete_number(number_id: uuid.UUID, request: Request, ctx: Manage) -> Response:
    tenant_id = ctx.require_tenant()
    async with AppDeps(request.app).db.tenant_session(tenant_id, ctx.actor) as s:
        deleted = (
            await s.execute(
                delete(inbound_numbers).where(inbound_numbers.c.id == number_id).returning(inbound_numbers.c.id)
            )
        ).scalar_one_or_none()
        if deleted is None:
            raise NotFound("Inbound number not found")
        await s.execute(
            delete(number_directory).where(
                number_directory.c.inbound_number_id == number_id, number_directory.c.tenant_id == tenant_id
            )
        )
        await audit(s, ctx.actor, "inbound_number.deleted", "inbound_number", number_id, tenant_id=tenant_id)
    return Response(status_code=204)
