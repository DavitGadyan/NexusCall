"""Leads: CRUD with filters, sort and keyset pagination."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError

from crm_service import paging
from crm_service.custom_fields import CustomFieldError, load_defs, validate_custom_fields
from crm_service.deps import audit, db
from crm_service.domain import (
    contact_updated,
    create_lead,
    find_by_addresses,
    insert_contact,
    load_contact,
    normalize_addresses,
)
from crm_service.idem import begin, require_key
from crm_service.schemas import ContactSummary, LeadIn, LeadOut, LeadPage, LeadPatch
from crm_service.tables import contacts, leads
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit

router = APIRouter(prefix="/api/v1/leads", tags=["leads"])
Read = Annotated[AuthContext, Depends(require_permission("leads:read"))]
Create = Annotated[AuthContext, Depends(require_permission("leads:create"))]
Update = Annotated[AuthContext, Depends(require_permission("leads:update"))]

SORTS: dict[str, tuple[Any, str]] = {
    "created_at": (leads.c.created_at, "datetime"),
    "updated_at": (leads.c.updated_at, "datetime"),
    "score": (func.coalesce(leads.c.score, -1), "int"),
}
SUMMARY_COLS = [
    contacts.c.first_name,
    contacts.c.last_name,
    contacts.c.company,
    contacts.c.email,
    contacts.c.phone_e164,
    contacts.c.status.label("contact_status"),
]


def _select() -> Select[Any]:
    return select(leads, *SUMMARY_COLS).select_from(leads.join(contacts, contacts.c.id == leads.c.contact_id))


def to_out(row: Any) -> LeadOut:
    data = {k: row[k] for k in LeadOut.model_fields if k != "contact"}
    contact = None
    if "contact_status" in row:
        contact = ContactSummary(
            id=row["contact_id"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            company=row["company"],
            email=row["email"],
            phone_e164=row["phone_e164"],
            status=row["contact_status"],
        )
    return LeadOut(**data, contact=contact)


async def _load(session: Any, lead_id: uuid.UUID) -> dict[str, Any]:
    row = (await session.execute(_select().where(leads.c.id == lead_id))).mappings().first()
    if row is None:
        raise NotFound("Lead not found")
    return dict(row)


@router.get("", response_model=LeadPage)
async def list_leads(
    request: Request,
    ctx: Read,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Literal["new", "contacted", "qualified", "unqualified", "booked", "won", "lost"] | None = None,
    source: str | None = None,
    owner_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    qualified: bool | None = None,
    sort: Literal["created_at", "updated_at", "score"] = "created_at",
    direction: Literal["asc", "desc"] = "desc",
) -> LeadPage:
    key, kind = SORTS[sort]
    stmt = _select()
    for col, value in (
        (leads.c.status, status),
        (leads.c.source, source),
        (leads.c.owner_id, owner_id),
        (leads.c.campaign_id, campaign_id),
        (leads.c.contact_id, contact_id),
        (leads.c.qualified, qualified),
    ):
        if value is not None:
            stmt = stmt.where(col == value)
    stmt = paging.apply(
        stmt, sort=sort, direction=direction, key=key, kind=kind, id_col=leads.c.id, cursor=cursor, limit=limit
    )
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    rows, next_cursor = paging.page(rows, limit, sort=sort, direction=direction)
    return LeadPage(items=[to_out(r) for r in rows], next_cursor=next_cursor)


@router.post("", status_code=201, response_model=LeadOut)
async def create(
    body: LeadIn, request: Request, ctx: Create, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    if (body.contact_id is None) == (body.contact is None):
        raise Unprocessable("Provide exactly one of contact_id or contact")
    tenant_id = ctx.require_tenant()
    try:
        async with db(request).tenant_session(tenant_id, ctx.actor) as s:
            op = await begin(
                s,
                tenant_id=tenant_id,
                operation="leads.create",
                key=require_key(idempotency_key),
                payload=body.model_dump(mode="json"),
            )
            if op.replay is not None:
                return op.replay
            if body.contact_id is not None:
                contact = await load_contact(s, body.contact_id, lock=True)
                if contact["status"] == "archived":
                    raise NotFound("Contact not found")
                contact_id = contact["id"]
            else:
                c = body.contact
                assert c is not None
                email, phone = normalize_addresses(c.email, c.phone, c.country or "US")
                by_email, by_phone = await find_by_addresses(s, email, phone, lock=True)
                if by_email and by_phone and by_email["id"] != by_phone["id"]:
                    raise Conflict("The email and the phone number belong to different contacts")
                existing = by_email or by_phone
                if existing is not None and existing["status"] != "archived":
                    contact_id = existing["id"]  # dedupe: reuse the known contact
                elif existing is not None:
                    raise Conflict("This contact was archived")
                else:
                    try:
                        custom = validate_custom_fields(c.custom_fields or {}, await load_defs(s))
                    except CustomFieldError as exc:
                        raise Unprocessable(str(exc)) from exc
                    values = c.model_dump(exclude={"tag_ids", "email", "phone", "custom_fields"})
                    row = await insert_contact(
                        s,
                        tenant_id=tenant_id,
                        values={**values, "email": email, "phone_e164": phone, "custom_fields": custom},
                        actor=ctx.actor,
                    )
                    contact_id = row["id"]
                    await contact_updated(s, tenant_id=tenant_id, actor=ctx.actor, contact_id=contact_id)
            values = body.model_dump(exclude={"contact_id", "contact"}, exclude_none=True)
            lead = await create_lead(s, tenant_id=tenant_id, contact_id=contact_id, actor=ctx.actor, values=values)
            await audit(s, ctx.actor, "lead.created", "lead", lead["id"], tenant_id=tenant_id)
            out = to_out(await _load(s, lead["id"]))
            return await op.done(201, out.model_dump(mode="json"), lead["id"])
    except IntegrityError as exc:
        raise Conflict("A contact with this email or phone already exists") from exc


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, request: Request, ctx: Read) -> LeadOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return to_out(await _load(s, lead_id))


@router.patch("/{lead_id}", response_model=LeadOut)
async def patch_lead(lead_id: uuid.UUID, body: LeadPatch, request: Request, ctx: Update) -> LeadOut:
    tenant_id = ctx.require_tenant()
    changes = {k: getattr(body, k) for k in body.model_fields_set - {"expected_version"}}
    if not changes:
        raise Unprocessable("No changes requested")
    if "status" in changes and changes["status"] is None:
        raise Unprocessable("status cannot be empty")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await _load(s, lead_id)
        updated = (
            await s.execute(
                leads.update()
                .where(leads.c.id == lead_id, leads.c.version == body.expected_version)
                .values(**changes, version=leads.c.version + 1, updated_at=func.now())
                .returning(leads.c.id)
            )
        ).first()
        if updated is None:
            raise Conflict("Lead was changed by someone else; reload and retry")
        await audit(s, ctx.actor, "lead.updated", "lead", lead_id, tenant_id=tenant_id, fields=sorted(changes))
        return to_out(await _load(s, lead_id))


@router.delete("/{lead_id}", status_code=204)
async def delete_lead(lead_id: uuid.UUID, request: Request, ctx: Update) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        result = await s.execute(delete(leads).where(leads.c.id == lead_id))
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Lead not found")
        await audit(s, ctx.actor, "lead.deleted", "lead", lead_id, tenant_id=tenant_id)
    return Response(status_code=204)
