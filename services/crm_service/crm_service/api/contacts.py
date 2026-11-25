"""Contacts: CRUD with filters, sort and keyset pagination; tags, consent, notes, contactability."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from crm_service import idem, paging
from crm_service.contactability import CHANNELS, evaluate
from crm_service.custom_fields import CustomFieldError, load_defs, validate_custom_fields
from crm_service.deps import audit, db
from crm_service.domain import (
    add_consent,
    contact_updated,
    find_by_addresses,
    insert_contact,
    load_contact,
    normalize_addresses,
    tag_names,
    update_contact,
)
from crm_service.schemas import (
    ChannelState,
    ConsentIn,
    ConsentOut,
    ContactabilityOut,
    ContactIn,
    ContactOut,
    ContactPage,
    ContactPatch,
    In,
    Items,
    NoteIn,
    NoteOut,
)
from crm_service.tables import consent_records, contact_notes, contact_tags, contacts, organizations, tags
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit

router = APIRouter(prefix="/api/v1/contacts", tags=["contacts"])
Read = Annotated[AuthContext, Depends(require_permission("leads:read"))]
Create = Annotated[AuthContext, Depends(require_permission("leads:create"))]
Update = Annotated[AuthContext, Depends(require_permission("leads:update"))]

SORTS: dict[str, tuple[Any, str]] = {
    "created_at": (contacts.c.created_at, "datetime"),
    "updated_at": (contacts.c.updated_at, "datetime"),
    "last_name": (func.coalesce(contacts.c.last_name, ""), "str"),
    "company": (func.coalesce(contacts.c.company, ""), "str"),
}


def to_out(row: Any, tag_list: list[str]) -> ContactOut:
    return ContactOut(**{k: row[k] for k in ContactOut.model_fields if k != "tags"}, tags=tag_list)


async def _check_refs(session: Any, body: ContactIn | ContactPatch) -> None:
    if body.organization_id is not None:
        found = (
            await session.execute(select(organizations.c.id).where(organizations.c.id == body.organization_id))
        ).first()
        if found is None:
            raise Unprocessable("Unknown organization")


def _contact_values(body: ContactIn | ContactPatch, fields: set[str]) -> dict[str, Any]:
    region = body.country or "US"
    values: dict[str, Any] = {}
    for name in fields:
        if name in ("expected_version", "tag_ids", "custom_fields", "phone", "email"):
            continue
        values[name] = getattr(body, name)
    if "email" in fields or "phone" in fields:
        email, phone = normalize_addresses(body.email, body.phone, region)
        if "email" in fields:
            values["email"] = email
        if "phone" in fields:
            values["phone_e164"] = phone
    return values


@router.get("", response_model=ContactPage)
async def list_contacts(
    request: Request,
    ctx: Read,
    limit: Limit = 50,
    cursor: Cursor = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status: Literal["active", "inactive", "archived"] | None = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    owner_id: uuid.UUID | None = None,
    source: Annotated[str | None, Query(max_length=64)] = None,
    country: Annotated[str | None, Query(pattern=r"^[A-Z]{2}$")] = None,
    sort: Literal["created_at", "updated_at", "last_name", "company"] = "created_at",
    direction: Literal["asc", "desc"] = "desc",
) -> ContactPage:
    key, kind = SORTS[sort]
    stmt: Select[Any] = select(contacts)
    stmt = stmt.where(contacts.c.status != "archived" if status is None else contacts.c.status == status)
    if q:
        stmt = stmt.where(
            contacts.c.first_name.icontains(q, autoescape=True)
            | contacts.c.last_name.icontains(q, autoescape=True)
            | contacts.c.company.icontains(q, autoescape=True)
            | contacts.c.email.icontains(q.lower(), autoescape=True)
            | contacts.c.phone_e164.contains(q, autoescape=True)
        )
    if tag:
        stmt = stmt.where(
            contacts.c.id.in_(
                select(contact_tags.c.contact_id)
                .select_from(contact_tags.join(tags, tags.c.id == contact_tags.c.tag_id))
                .where(tags.c.name == tag)
            )
        )
    for col, value in ((contacts.c.owner_id, owner_id), (contacts.c.source, source), (contacts.c.country, country)):
        if value is not None:
            stmt = stmt.where(col == value)
    stmt = paging.apply(
        stmt, sort=sort, direction=direction, key=key, kind=kind, id_col=contacts.c.id, cursor=cursor, limit=limit
    )
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
        rows, next_cursor = paging.page(rows, limit, sort=sort, direction=direction)
        names = await tag_names(s, [r["id"] for r in rows])
    return ContactPage(items=[to_out(r, names[r["id"]]) for r in rows], next_cursor=next_cursor)


@router.post("", status_code=201, response_model=ContactOut)
async def create_contact(
    body: ContactIn,
    request: Request,
    ctx: Create,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    tenant_id = ctx.require_tenant()
    values = _contact_values(body, set(ContactIn.model_fields) - {"tag_ids"})
    if not any(values.get(k) for k in ("email", "phone_e164", "first_name", "last_name", "company")):
        raise Unprocessable("A contact needs a name, a company, an email or a phone number")
    try:
        async with db(request).tenant_session(tenant_id, ctx.actor) as s:
            op = await idem.begin(
                s,
                tenant_id=tenant_id,
                operation="contacts.create",
                key=idem.require_key(idempotency_key),
                payload=body.model_dump(mode="json"),
            )
            if op.replay is not None:
                return op.replay
            await _check_refs(s, body)
            by_email, by_phone = await find_by_addresses(s, values.get("email"), values.get("phone_e164"))
            if by_email or by_phone:
                existing = by_email or by_phone
                raise Conflict(
                    "A contact with this email or phone already exists",
                    extra={"contact_id": str(existing["id"])},  # type: ignore[index]
                )
            try:
                values["custom_fields"] = validate_custom_fields(body.custom_fields or {}, await load_defs(s))
            except CustomFieldError as exc:
                raise Unprocessable(str(exc)) from exc
            row = await insert_contact(s, tenant_id=tenant_id, values=values, actor=ctx.actor)
            if body.tag_ids:
                await _add_tags(s, tenant_id, row["id"], body.tag_ids)
            await contact_updated(s, tenant_id=tenant_id, actor=ctx.actor, contact_id=row["id"])
            await audit(s, ctx.actor, "contact.created", "contact", row["id"], tenant_id=tenant_id)
            out = to_out(row, (await tag_names(s, [row["id"]]))[row["id"]])
            return await op.done(201, out.model_dump(mode="json"), row["id"])
    except IntegrityError as exc:
        raise Conflict("A contact with this email or phone already exists") from exc


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(contact_id: uuid.UUID, request: Request, ctx: Read) -> ContactOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = await load_contact(s, contact_id)
        return to_out(row, (await tag_names(s, [contact_id]))[contact_id])


@router.patch("/{contact_id}", response_model=ContactOut)
async def patch_contact(contact_id: uuid.UUID, body: ContactPatch, request: Request, ctx: Update) -> ContactOut:
    tenant_id = ctx.require_tenant()
    fields = set(body.model_fields_set) - {"expected_version"}
    if not fields:
        raise Unprocessable("No changes requested")
    try:
        async with db(request).tenant_session(tenant_id, ctx.actor) as s:
            current = await load_contact(s, contact_id, lock=True)
            if current["status"] == "archived":
                raise NotFound("Contact not found")
            await _check_refs(s, body)
            changes = _contact_values(body, fields)
            if "status" in changes and changes["status"] is None:
                raise Unprocessable("status cannot be empty")
            if "custom_fields" in fields:
                try:
                    changes["custom_fields"] = validate_custom_fields(
                        body.custom_fields or {}, await load_defs(s), existing=current["custom_fields"]
                    )
                except CustomFieldError as exc:
                    raise Unprocessable(str(exc)) from exc
            row = await update_contact(s, contact_id, changes, expected_version=body.expected_version)
            if row is None:
                raise Conflict("Contact was changed by someone else; reload and retry")
            await contact_updated(s, tenant_id=tenant_id, actor=ctx.actor, contact_id=contact_id)
            await audit(
                s, ctx.actor, "contact.updated", "contact", contact_id, tenant_id=tenant_id, fields=sorted(fields)
            )
            return to_out(row, (await tag_names(s, [contact_id]))[contact_id])
    except IntegrityError as exc:
        raise Conflict("Another contact already uses this email or phone") from exc


@router.delete("/{contact_id}", status_code=204)
async def archive_contact(contact_id: uuid.UUID, request: Request, ctx: Update) -> Response:
    """Archives the contact (never contactable again). Consent and suppression evidence is retained."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await load_contact(s, contact_id, lock=True)
        if current["status"] != "archived":
            await update_contact(s, contact_id, {"status": "archived"})
            await contact_updated(s, tenant_id=tenant_id, actor=ctx.actor, contact_id=contact_id)
            await audit(s, ctx.actor, "contact.archived", "contact", contact_id, tenant_id=tenant_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------------------------- tags
class TagIdsIn(In):
    tag_ids: list[uuid.UUID]


async def _add_tags(session: Any, tenant_id: uuid.UUID, contact_id: uuid.UUID, tag_ids: list[uuid.UUID]) -> None:
    known = set((await session.execute(select(tags.c.id).where(tags.c.id.in_(tag_ids)))).scalars())
    if known != set(tag_ids):
        raise Unprocessable("Unknown tag")
    for tag_id in tag_ids:
        await session.execute(
            insert(contact_tags)
            .values(tenant_id=tenant_id, contact_id=contact_id, tag_id=tag_id)
            .on_conflict_do_nothing()
        )


@router.post("/{contact_id}/tags", response_model=ContactOut)
async def add_tags(contact_id: uuid.UUID, body: TagIdsIn, request: Request, ctx: Update) -> ContactOut:
    if not 1 <= len(body.tag_ids) <= 20:
        raise Unprocessable("Between 1 and 20 tags per request")
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await load_contact(s, contact_id, lock=True)
        await _add_tags(s, tenant_id, contact_id, body.tag_ids)
        row = await update_contact(s, contact_id, {})
        await contact_updated(s, tenant_id=tenant_id, actor=ctx.actor, contact_id=contact_id)
        return to_out(row, (await tag_names(s, [contact_id]))[contact_id])


@router.delete("/{contact_id}/tags/{tag_id}", status_code=204)
async def remove_tag(contact_id: uuid.UUID, tag_id: uuid.UUID, request: Request, ctx: Update) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await load_contact(s, contact_id, lock=True)
        result = await s.execute(
            contact_tags.delete().where(contact_tags.c.contact_id == contact_id, contact_tags.c.tag_id == tag_id)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFound("Tag is not on this contact")
        await update_contact(s, contact_id, {})
        await contact_updated(s, tenant_id=tenant_id, actor=ctx.actor, contact_id=contact_id)
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------- consent
def _consent_out(row: Any) -> ConsentOut:
    return ConsentOut(**{k: row[k] for k in ConsentOut.model_fields})


@router.get("/{contact_id}/consents", response_model=Items[ConsentOut])
async def list_consents(contact_id: uuid.UUID, request: Request, ctx: Read) -> Items[ConsentOut]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        await load_contact(s, contact_id)
        rows = (
            (
                await s.execute(
                    select(consent_records)
                    .where(consent_records.c.contact_id == contact_id)
                    .order_by(consent_records.c.captured_at.desc(), consent_records.c.created_at.desc())
                    .limit(200)
                )
            )
            .mappings()
            .all()
        )
    return Items[ConsentOut](items=[_consent_out(r) for r in rows])


@router.post("/{contact_id}/consents", status_code=201, response_model=ConsentOut)
async def record_consent(contact_id: uuid.UUID, body: ConsentIn, request: Request, ctx: Update) -> ConsentOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await load_contact(s, contact_id, lock=True)
        consent_id = await add_consent(
            s, tenant_id=tenant_id, contact_id=contact_id, actor=ctx.actor, **body.model_dump(exclude_none=True)
        )
        await contact_updated(s, tenant_id=tenant_id, actor=ctx.actor, contact_id=contact_id)
        await audit(
            s,
            ctx.actor,
            "consent.recorded",
            "contact",
            contact_id,
            tenant_id=tenant_id,
            channel=body.channel,
            status=body.status,
            lawful_basis=body.lawful_basis,
        )
        row = (await s.execute(select(consent_records).where(consent_records.c.id == consent_id))).mappings().one()
    return _consent_out(row)


# --------------------------------------------------------------------------------------------- notes
def _note_out(row: Any) -> NoteOut:
    return NoteOut(**{k: row[k] for k in NoteOut.model_fields})


@router.get("/{contact_id}/notes", response_model=Items[NoteOut])
async def list_notes(contact_id: uuid.UUID, request: Request, ctx: Read) -> Items[NoteOut]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        await load_contact(s, contact_id)
        rows = (
            (
                await s.execute(
                    select(contact_notes)
                    .where(contact_notes.c.contact_id == contact_id)
                    .order_by(contact_notes.c.created_at.desc())
                    .limit(200)
                )
            )
            .mappings()
            .all()
        )
    return Items[NoteOut](items=[_note_out(r) for r in rows])


@router.post("/{contact_id}/notes", status_code=201, response_model=NoteOut)
async def add_note(contact_id: uuid.UUID, body: NoteIn, request: Request, ctx: Update) -> NoteOut:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await load_contact(s, contact_id)
        row = (
            (
                await s.execute(
                    insert(contact_notes)
                    .values(
                        tenant_id=tenant_id,
                        contact_id=contact_id,
                        text=body.text,
                        source_type="user",
                        source_id=ctx.actor.id,
                        created_by=str(ctx.actor),
                    )
                    .returning(contact_notes)
                )
            )
            .mappings()
            .one()
        )
    return _note_out(row)


# ----------------------------------------------------------------------------------- contactability
@router.get("/{contact_id}/contactability", response_model=ContactabilityOut)
async def contactability(contact_id: uuid.UUID, request: Request, ctx: Read) -> ContactabilityOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = await load_contact(s, contact_id)
        states = {ch: (await evaluate(s, [row], ch))[contact_id] for ch in CHANNELS}
    return ContactabilityOut(
        contact_id=contact_id,
        phone=ChannelState(allowed=not states["phone"], reasons=states["phone"]),
        email=ChannelState(allowed=not states["email"], reasons=states["email"]),
    )
