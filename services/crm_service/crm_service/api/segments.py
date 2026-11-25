"""Segments: saved rule DSL documents with a preview count."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ColumnElement, and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from crm_service.contactability import contactable_clause
from crm_service.custom_fields import load_defs
from crm_service.deps import audit, db
from crm_service.schemas import In, Items, clean_text
from crm_service.segment_dsl import SegmentError, compile_rules
from crm_service.tables import contacts, segments
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound, Unprocessable

router = APIRouter(prefix="/api/v1/segments", tags=["segments"])
Read = Annotated[AuthContext, Depends(require_permission("campaigns:read"))]
Create = Annotated[AuthContext, Depends(require_permission("campaigns:create"))]
Manage = Annotated[AuthContext, Depends(require_permission("campaigns:manage"))]


class SegmentIn(In):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    rules: dict[str, Any]

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        cleaned = clean_text(v)
        if not cleaned:
            raise ValueError("name is required")
        return cleaned


class SegmentPatch(In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    rules: dict[str, Any] | None = None
    expected_version: int


class PreviewIn(In):
    rules: dict[str, Any]
    channel: Literal["phone", "email"] | None = None


class SegmentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    rules: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class PreviewOut(BaseModel):
    matching: int
    contactable: int | None
    channel: str | None


def _out(row: Any) -> SegmentOut:
    return SegmentOut(**{k: row[k] for k in SegmentOut.model_fields})


async def segment_clause(session: AsyncSession, rules: Any) -> ColumnElement[bool]:
    """Compile against the tenant's current custom field definitions. Archived contacts never match."""
    try:
        clause = compile_rules(rules, await load_defs(session))
    except SegmentError as exc:
        raise Unprocessable(f"Invalid segment rules: {exc}") from exc
    return and_(contacts.c.status != "archived", clause)


async def preview(session: AsyncSession, rules: Any, channel: str | None) -> PreviewOut:
    clause = await segment_clause(session, rules)
    matching = (await session.execute(select(func.count()).select_from(contacts).where(clause))).scalar_one()
    contactable = None
    if channel:
        contactable = (
            await session.execute(select(func.count()).select_from(contacts).where(clause, contactable_clause(channel)))
        ).scalar_one()
    return PreviewOut(matching=matching, contactable=contactable, channel=channel)


async def load_segment(session: AsyncSession, segment_id: uuid.UUID) -> dict[str, Any]:
    row = (await session.execute(select(segments).where(segments.c.id == segment_id))).mappings().first()
    if row is None:
        raise NotFound("Segment not found")
    return dict(row)


@router.get("", response_model=Items[SegmentOut])
async def list_segments(request: Request, ctx: Read) -> Items[SegmentOut]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(select(segments).order_by(segments.c.name).limit(500))).mappings().all()
    return Items[SegmentOut](items=[_out(r) for r in rows])


@router.post("/preview", response_model=PreviewOut)
async def preview_rules(body: PreviewIn, request: Request, ctx: Read) -> PreviewOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return await preview(s, body.rules, body.channel)


@router.post("", status_code=201, response_model=SegmentOut)
async def create_segment(body: SegmentIn, request: Request, ctx: Create) -> SegmentOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await segment_clause(s, body.rules)  # validate before storing
        row = (
            (
                await s.execute(
                    insert(segments)
                    .values(tenant_id=tenant_id, created_by=str(ctx.actor), **body.model_dump())
                    .on_conflict_do_nothing(constraint="uq_segments_name")
                    .returning(segments)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("A segment with this name already exists")
        await audit(s, ctx.actor, "segment.created", "segment", row["id"], tenant_id=tenant_id)
    return _out(row)


@router.get("/{segment_id}", response_model=SegmentOut)
async def get_segment(segment_id: uuid.UUID, request: Request, ctx: Read) -> SegmentOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return _out(await load_segment(s, segment_id))


@router.get("/{segment_id}/preview", response_model=PreviewOut)
async def preview_segment(
    segment_id: uuid.UUID, request: Request, ctx: Read, channel: Literal["phone", "email"] | None = None
) -> PreviewOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return await preview(s, (await load_segment(s, segment_id))["rules"], channel)


@router.patch("/{segment_id}", response_model=SegmentOut)
async def patch_segment(segment_id: uuid.UUID, body: SegmentPatch, request: Request, ctx: Create) -> SegmentOut:
    tenant_id = ctx.require_tenant()
    changes = {k: getattr(body, k) for k in body.model_fields_set - {"expected_version"}}
    if not changes or any(changes.get(k) is None for k in ("name", "rules") if k in changes):
        raise Unprocessable("No valid changes requested")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await load_segment(s, segment_id)
        if "rules" in changes:
            await segment_clause(s, changes["rules"])
        row = (
            (
                await s.execute(
                    update(segments)
                    .where(segments.c.id == segment_id, segments.c.version == body.expected_version)
                    .values(**changes, version=segments.c.version + 1, updated_at=func.now())
                    .returning(segments)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("Segment was changed by someone else; reload and retry")
        await audit(s, ctx.actor, "segment.updated", "segment", segment_id, tenant_id=tenant_id, fields=sorted(changes))
    return _out(row)


@router.delete("/{segment_id}", status_code=204)
async def delete_segment(segment_id: uuid.UUID, request: Request, ctx: Manage) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        if (await s.execute(delete(segments).where(segments.c.id == segment_id))).rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Segment not found")
        await audit(s, ctx.actor, "segment.deleted", "segment", segment_id, tenant_id=tenant_id)
    return Response(status_code=204)
