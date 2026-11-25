"""Tags, custom field definitions and organizations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from crm_service.deps import audit, db
from crm_service.schemas import In, Items, clean_text
from crm_service.tables import contacts, custom_field_defs, organizations, tags
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound, Unprocessable

router = APIRouter(prefix="/api/v1", tags=["catalog"])
Read = Annotated[AuthContext, Depends(require_permission("leads:read"))]
Update = Annotated[AuthContext, Depends(require_permission("leads:update"))]


# ---------------------------------------------------------------------------------------------- tags
class TagIn(In):
    name: str = Field(min_length=1, max_length=64)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        cleaned = clean_text(v)
        if not cleaned:
            raise ValueError("name is required")
        return cleaned


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str | None
    created_at: datetime


@router.get("/tags", response_model=Items[TagOut])
async def list_tags(request: Request, ctx: Read) -> Items[TagOut]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(select(tags).order_by(tags.c.name).limit(1000))).mappings().all()
    return Items[TagOut](items=[TagOut(**{k: r[k] for k in TagOut.model_fields}) for r in rows])


@router.post("/tags", status_code=201, response_model=TagOut)
async def create_tag(body: TagIn, request: Request, ctx: Update) -> TagOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (
                await s.execute(
                    insert(tags)
                    .values(tenant_id=tenant_id, name=body.name, color=body.color)
                    .on_conflict_do_nothing(constraint="uq_tags_name")
                    .returning(tags)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("A tag with this name already exists")
        await audit(s, ctx.actor, "tag.created", "tag", row["id"], tenant_id=tenant_id)
    return TagOut(**{k: row[k] for k in TagOut.model_fields})


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(tag_id: uuid.UUID, request: Request, ctx: Update) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        if (await s.execute(delete(tags).where(tags.c.id == tag_id))).rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Tag not found")
        await audit(s, ctx.actor, "tag.deleted", "tag", tag_id, tenant_id=tenant_id)
    return Response(status_code=204)


# ------------------------------------------------------------------------------------- custom fields
class FieldDefIn(In):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    type: Literal["text", "number", "boolean", "date", "select"]
    options: list[str] = Field(default_factory=list, max_length=100)
    required: bool = False

    @model_validator(mode="after")
    def _options(self) -> FieldDefIn:
        if self.type == "select" and not self.options:
            raise ValueError("select fields need options")
        if self.type != "select" and self.options:
            raise ValueError("only select fields take options")
        if any(not o or len(o) > 100 or clean_text(o) != o for o in self.options):
            raise ValueError("options must be 1..100 printable characters")
        return self


class FieldDefPatch(In):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    options: list[str] | None = Field(default=None, max_length=100)
    required: bool | None = None
    expected_version: int


class FieldDefOut(BaseModel):
    id: uuid.UUID
    key: str
    label: str
    type: str
    options: list[str]
    required: bool
    version: int
    created_at: datetime
    updated_at: datetime


def _field_out(row: Any) -> FieldDefOut:
    return FieldDefOut(**{k: row[k] for k in FieldDefOut.model_fields})


@router.get("/custom-fields", response_model=Items[FieldDefOut])
async def list_fields(request: Request, ctx: Read) -> Items[FieldDefOut]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(select(custom_field_defs).order_by(custom_field_defs.c.key))).mappings().all()
    return Items[FieldDefOut](items=[_field_out(r) for r in rows])


@router.post("/custom-fields", status_code=201, response_model=FieldDefOut)
async def create_field(body: FieldDefIn, request: Request, ctx: Update) -> FieldDefOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        count = (await s.execute(select(func.count()).select_from(custom_field_defs))).scalar_one()
        if count >= 50:
            raise Unprocessable("At most 50 custom fields per workspace")
        row = (
            (
                await s.execute(
                    insert(custom_field_defs)
                    .values(tenant_id=tenant_id, **body.model_dump())
                    .on_conflict_do_nothing(constraint="uq_custom_field_defs_key")
                    .returning(custom_field_defs)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("A custom field with this key already exists")
        await audit(s, ctx.actor, "custom_field.created", "custom_field", row["id"], tenant_id=tenant_id, key=body.key)
    return _field_out(row)


@router.patch("/custom-fields/{field_id}", response_model=FieldDefOut)
async def patch_field(field_id: uuid.UUID, body: FieldDefPatch, request: Request, ctx: Update) -> FieldDefOut:
    tenant_id = ctx.require_tenant()
    changes = {k: getattr(body, k) for k in body.model_fields_set - {"expected_version"}}
    if not changes:
        raise Unprocessable("No changes requested")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = (
            (await s.execute(select(custom_field_defs).where(custom_field_defs.c.id == field_id))).mappings().first()
        )
        if current is None:
            raise NotFound("Custom field not found")
        if "options" in changes:
            try:
                FieldDefIn(
                    key=current["key"], label=current["label"], type=current["type"], options=changes["options"] or []
                )
            except ValidationError as exc:
                raise Unprocessable("Invalid options for this field type") from exc
        row = (
            (
                await s.execute(
                    update(custom_field_defs)
                    .where(custom_field_defs.c.id == field_id, custom_field_defs.c.version == body.expected_version)
                    .values(**changes, version=custom_field_defs.c.version + 1, updated_at=func.now())
                    .returning(custom_field_defs)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("Custom field was changed by someone else; reload and retry")
        await audit(s, ctx.actor, "custom_field.updated", "custom_field", field_id, tenant_id=tenant_id)
    return _field_out(row)


@router.delete("/custom-fields/{field_id}", status_code=204)
async def delete_field(field_id: uuid.UUID, request: Request, ctx: Update) -> Response:
    """Removes the definition. Existing values stay in contacts but can no longer be set or used in segments."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        result = await s.execute(delete(custom_field_defs).where(custom_field_defs.c.id == field_id))
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Custom field not found")
        await audit(s, ctx.actor, "custom_field.deleted", "custom_field", field_id, tenant_id=tenant_id)
    return Response(status_code=204)


# ------------------------------------------------------------------------------------- organizations
class OrganizationIn(In):
    name: str = Field(min_length=1, max_length=200)
    domain: str | None = Field(default=None, max_length=253, pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$")

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        cleaned = clean_text(v)
        if not cleaned:
            raise ValueError("name is required")
        return cleaned


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    domain: str | None
    version: int
    created_at: datetime


@router.get("/organizations", response_model=Items[OrganizationOut])
async def list_organizations(request: Request, ctx: Read) -> Items[OrganizationOut]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(select(organizations).order_by(organizations.c.name).limit(1000))).mappings().all()
    return Items[OrganizationOut](
        items=[OrganizationOut(**{k: r[k] for k in OrganizationOut.model_fields}) for r in rows]
    )


@router.post("/organizations", status_code=201, response_model=OrganizationOut)
async def create_organization(body: OrganizationIn, request: Request, ctx: Update) -> OrganizationOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (
                await s.execute(
                    insert(organizations)
                    .values(tenant_id=tenant_id, **body.model_dump())
                    .on_conflict_do_nothing(constraint="uq_organizations_name")
                    .returning(organizations)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("An organization with this name already exists")
        await audit(s, ctx.actor, "organization.created", "organization", row["id"], tenant_id=tenant_id)
    return OrganizationOut(**{k: row[k] for k in OrganizationOut.model_fields})


@router.delete("/organizations/{organization_id}", status_code=204)
async def delete_organization(organization_id: uuid.UUID, request: Request, ctx: Update) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await s.execute(
            update(contacts)
            .where(contacts.c.organization_id == organization_id)
            .values(organization_id=None, version=contacts.c.version + 1, updated_at=func.now())
        )
        result = await s.execute(delete(organizations).where(organizations.c.id == organization_id))
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Organization not found")
        await audit(s, ctx.actor, "organization.deleted", "organization", organization_id, tenant_id=tenant_id)
    return Response(status_code=204)
