"""Templates with append-only versions (`emails:campaigns:manage`). Content is validated (links, variables,
CR/LF) and sanitized with bleach before it is stored; preview renders with sample values; a test send goes
through the normal pipeline (suppression, quotas, kill switches) as a transactional message."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_service.api.common import IdemHeader, In, Manage, Page, db, idem_begin, require_key
from email_service.api.domains import load_sender
from email_service.content import VARIABLES, TemplateError, prepare_template, render_template
from email_service.deps import audit, job_context
from email_service.messages import insert_message, task_for
from email_service.security import address_hash, domain_of, new_rfc_message_id, valid_address
from email_service.tables import email_messages, template_versions, templates
from nexus_core.errors import Conflict, NotFound, TooManyRequests, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/email/templates", tags=["email-templates"])
SAMPLE_VALUES = {"first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace", "sender_name": "Acme"}


class TemplateContent(In):
    subject: str = Field(min_length=1, max_length=998)
    html: str = Field(min_length=1, max_length=200_000)
    text: str | None = Field(default=None, max_length=200_000)


class TemplateIn(TemplateContent):
    name: str = Field(min_length=1, max_length=120)
    category: Literal["campaign", "transactional"] = "campaign"


class TemplateVersionIn(TemplateContent):
    expected_version: int = Field(ge=1)


class VersionOut(BaseModel):
    version: int
    subject: str
    html: str
    text: str
    variables: list[str]
    created_at: str
    created_by: str | None


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    status: str
    current_version: int
    version: int
    created_at: str
    updated_at: str
    current: VersionOut | None = None


class TemplateList(Page):
    items: list[TemplateOut]


class VersionList(BaseModel):
    items: list[VersionOut]


class PreviewIn(In):
    version: int | None = Field(default=None, ge=1)
    variables: dict[str, str] = Field(default_factory=dict, max_length=10)

    @field_validator("variables")
    @classmethod
    def _known(cls, v: dict[str, str]) -> dict[str, str]:
        if set(v) - (set(VARIABLES) - {"unsubscribe_url"}) or any(len(x) > 200 for x in v.values()):
            raise ValueError("unknown variable or value too long")
        return v


class PreviewOut(BaseModel):
    version: int
    subject: str
    html: str
    text: str


class TestSendIn(PreviewIn):
    sender_id: uuid.UUID
    to: str = Field(max_length=320)

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        if not valid_address(v):
            raise ValueError("invalid recipient address")
        return v


class TestSendOut(BaseModel):
    message_id: uuid.UUID
    state: str


def _version_out(row: Any) -> VersionOut:
    return VersionOut(
        version=row["version"],
        subject=row["subject"],
        html=row["html"],
        text=row["text"],
        variables=list(row["variables"]),
        created_at=row["created_at"].isoformat(),
        created_by=row["created_by"],
    )


def _out(row: Any, current: Any = None) -> TemplateOut:
    return TemplateOut(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        status=row["status"],
        current_version=row["current_version"],
        version=row["version"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        current=_version_out(current) if current is not None else None,
    )


def _prepare(category: str, body: TemplateContent) -> Any:
    try:
        return prepare_template(category=category, subject=body.subject, html=body.html, text=body.text)
    except TemplateError as exc:
        raise Unprocessable("Template content is not allowed", extra={"problems": exc.problems}) from exc


async def load_template(session: Any, template_id: uuid.UUID) -> Any:
    row = (await session.execute(select(templates).where(templates.c.id == template_id))).mappings().first()
    if row is None:
        raise NotFound("Template not found")
    return row


async def load_version(session: Any, template_id: uuid.UUID, version: int) -> Any:
    row = (
        (
            await session.execute(
                select(template_versions).where(
                    template_versions.c.template_id == template_id, template_versions.c.version == version
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Template version not found")
    return row


@router.get("", response_model=TemplateList)
async def list_templates(request: Request, ctx: Manage, limit: Limit = 50, cursor: Cursor = None) -> TemplateList:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        stmt = apply_keyset(select(templates), templates.c.created_at, templates.c.id, cursor, limit)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, nxt = build_page(rows, limit)
    return TemplateList(items=[_out(r) for r in page], next_cursor=nxt)


@router.post("", status_code=201, response_model=TemplateOut)
async def create_template(body: TemplateIn, request: Request, ctx: Manage, idempotency_key: IdemHeader = None) -> Any:
    tenant_id = ctx.require_tenant()
    clean = _prepare(body.category, body)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem_begin(s, tenant_id=tenant_id, operation="templates.create", key=idempotency_key,
                              payload=body.model_dump())  # fmt: skip
        if op.replay is not None:
            return op.replay
        row = (
            (
                await s.execute(
                    insert(templates)
                    .values(tenant_id=tenant_id, name=body.name, category=body.category, created_by=str(ctx.actor))
                    .on_conflict_do_nothing(constraint="uq_templates_name")
                    .returning(templates)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("A template with this name already exists")
        version = (
            (
                await s.execute(
                    insert(template_versions)
                    .values(
                        tenant_id=tenant_id,
                        template_id=row["id"],
                        version=1,
                        subject=clean.subject,
                        html=clean.html,
                        text=clean.text,
                        variables=clean.variables,
                        created_by=str(ctx.actor),
                    )
                    .returning(template_versions)
                )
            )
            .mappings()
            .one()
        )
        await audit(s, ctx.actor, "email_template.created", "email_template", row["id"], tenant_id=tenant_id)
        return await op.done(201, _out(row, version).model_dump(mode="json"), row["id"])


@router.get("/{template_id}", response_model=TemplateOut)
async def get_template(template_id: uuid.UUID, request: Request, ctx: Manage) -> TemplateOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = await load_template(s, template_id)
        return _out(row, await load_version(s, template_id, row["current_version"]))


@router.put("/{template_id}", response_model=TemplateOut)
async def new_version(template_id: uuid.UUID, body: TemplateVersionIn, request: Request, ctx: Manage) -> TemplateOut:
    """Edits never change history: each save appends version N+1 (campaigns keep the version they pinned)."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = await load_template(s, template_id)
        clean = _prepare(row["category"], body)
        bumped = (
            (
                await s.execute(
                    update(templates)
                    .where(templates.c.id == template_id, templates.c.version == body.expected_version)
                    .values(
                        current_version=templates.c.current_version + 1,
                        version=templates.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(templates)
                )
            )
            .mappings()
            .first()
        )
        if bumped is None:
            raise Conflict("Template was changed by someone else; reload and retry")
        version = (
            (
                await s.execute(
                    insert(template_versions)
                    .values(
                        tenant_id=tenant_id,
                        template_id=template_id,
                        version=bumped["current_version"],
                        subject=clean.subject,
                        html=clean.html,
                        text=clean.text,
                        variables=clean.variables,
                        created_by=str(ctx.actor),
                    )
                    .returning(template_versions)
                )
            )
            .mappings()
            .one()
        )
        await audit(s, ctx.actor, "email_template.versioned", "email_template", template_id, tenant_id=tenant_id,
                    version=bumped["current_version"])  # fmt: skip
        return _out(bumped, version)


@router.get("/{template_id}/versions", response_model=VersionList)
async def list_versions(template_id: uuid.UUID, request: Request, ctx: Manage) -> VersionList:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        await load_template(s, template_id)
        rows = (
            (
                await s.execute(
                    select(template_versions)
                    .where(template_versions.c.template_id == template_id)
                    .order_by(template_versions.c.version.desc())
                    .limit(200)
                )
            )
            .mappings()
            .all()
        )
    return VersionList(items=[_version_out(r) for r in rows])


async def _render(s: Any, template_id: uuid.UUID, body: PreviewIn, *, sender_name: str) -> tuple[Any, Any]:
    row = await load_template(s, template_id)
    version = await load_version(s, template_id, body.version or row["current_version"])
    values = {**SAMPLE_VALUES, "sender_name": sender_name, **body.variables}
    rendered = render_template(
        category=row["category"],
        subject=version["subject"],
        html=version["html"],
        text=version["text"],
        values=values,
        unsubscribe_url="https://unsubscribe.invalid/preview" if row["category"] == "campaign" else None,
    )
    return version, rendered


@router.post("/{template_id}/preview", response_model=PreviewOut)
async def preview(template_id: uuid.UUID, body: PreviewIn, request: Request, ctx: Manage) -> PreviewOut:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        version, rendered = await _render(s, template_id, body, sender_name=SAMPLE_VALUES["sender_name"])
    return PreviewOut(version=version["version"], subject=rendered.subject, html=rendered.html, text=rendered.text)


@router.post("/{template_id}/test-send", status_code=202, response_model=TestSendOut)
async def test_send(
    template_id: uuid.UUID, body: TestSendIn, request: Request, ctx: Manage, idempotency_key: IdemHeader = None
) -> Any:
    """Sends one rendered copy to `to` as a transactional message. Bounded per tenant per hour."""
    tenant_id = ctx.require_tenant()
    key = require_key(idempotency_key)
    settings = request.app.state.settings
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem_begin(s, tenant_id=tenant_id, operation=f"templates.test_send:{template_id}", key=key,
                              payload=body.model_dump(mode="json"))  # fmt: skip
        if op.replay is not None:
            return op.replay
        since = datetime.now(UTC) - timedelta(hours=1)
        recent = (
            await s.execute(
                select(func.count()).where(
                    email_messages.c.source_type == "template_test", email_messages.c.created_at > since
                )
            )
        ).scalar_one()
        if recent >= settings.test_sends_per_hour:
            raise TooManyRequests("Test-send limit reached; try again later")
        sender = await load_sender(s, body.sender_id)
        if sender["status"] != "active":
            raise Conflict("Sender identity is disabled")
        version, rendered = await _render(s, template_id, body, sender_name=sender["from_name"])
        message_id = uuid.uuid4()
        await insert_message(
            s,
            tenant_id,
            id=message_id,
            category="transactional",
            state="queued",
            sender_id=sender["id"],
            template_id=template_id,
            template_version=version["version"],
            from_address=sender["from_address"],
            from_name=sender["from_name"],
            reply_to=sender["reply_to"],
            to_address=body.to,
            to_hash=address_hash(body.to),
            subject=f"[TEST] {rendered.subject}"[:998],
            body_text=rendered.text,
            body_html=rendered.html,
            rfc_message_id=new_rfc_message_id(domain_of(sender["from_address"])),
            idempotency_key=str(message_id),
            source_type="template_test",
            source_id=str(template_id),
            source_version=version["version"],
            queued_at=func.now(),
        )
        await audit(s, ctx.actor, "email_template.test_sent", "email_template", template_id, tenant_id=tenant_id,
                    message_id=str(message_id))  # fmt: skip
        result = await op.done(202, {"message_id": str(message_id), "state": "queued"}, message_id)
    deps = request.app.state
    await deps.enqueue(
        task_for("transactional"),
        settings.transactional_queue,
        job_context(tenant_id, ctx.actor),
        message_id=str(message_id),
    )
    return result
