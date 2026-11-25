"""Inbound mailboxes (`emails:campaigns:manage`). Each address maps to exactly one tenant platform-wide
through `mailbox_directory`, which is the trusted configuration inbound mail is routed by."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_inbox_service.api.common import db
from email_inbox_service.api.schemas import MailboxIn, MailboxOut, MailboxPage, MailboxPatch
from email_inbox_service.deps import audit, request_deps
from email_inbox_service.mime import address_hash, normalize_address
from email_inbox_service.tables import idempotency_keys, mailbox_directory, mailboxes
from nexus_core.authz import AuthContext, require_permission
from nexus_core.clients import UpstreamError, UpstreamUnavailable
from nexus_core.errors import Conflict, NotFound, ServiceUnavailable, Unprocessable
from nexus_core.idempotency import IdempotencyStore
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/email/mailboxes", tags=["mailboxes"])
Manager = Annotated[AuthContext, Depends(require_permission("emails:campaigns:manage"))]
idem = IdempotencyStore(idempotency_keys)


async def _check_agent(request: Request, ctx: AuthContext, agent_id: uuid.UUID) -> None:
    """The drafting agent must be an active `email` agent of this tenant (ai-agent-service decides)."""
    try:
        agent = (
            await request_deps(request)
            .client("ai-agent-service")
            .get_json(f"/internal/v1/agents/{agent_id}", tenant_id=ctx.require_tenant(), actor=ctx.actor)
        )
    except UpstreamUnavailable as exc:
        raise ServiceUnavailable("Agent service unavailable") from exc
    except UpstreamError as exc:
        if exc.is_not_found:
            raise Unprocessable("Unknown agent") from exc
        raise
    if agent.get("kind") != "email" or agent.get("status") != "active":
        raise Unprocessable("The agent must be an active email agent")


@router.get("", response_model=MailboxPage)
async def list_mailboxes(request: Request, ctx: Manager, limit: Limit = 50, cursor: Cursor = None) -> Any:
    tenant_id = ctx.require_tenant()
    stmt = apply_keyset(select(mailboxes), mailboxes.c.created_at, mailboxes.c.id, cursor, limit)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
    page, next_cursor = build_page(rows, limit)
    return {"items": page, "next_cursor": next_cursor}


@router.post("", response_model=MailboxOut, status_code=201)
async def create_mailbox(
    body: MailboxIn,
    request: Request,
    ctx: Manager,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    tenant_id = ctx.require_tenant()
    address = normalize_address(body.address)
    if address is None or address != body.address.strip().lower():
        raise Unprocessable("Enter a single plain email address")
    if body.agent_id is not None:
        await _check_agent(request, ctx, body.agent_id)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = None
        if idempotency_key:
            op = await idem.begin(
                s,
                tenant_id=tenant_id,
                operation="mailbox.create",
                key=idempotency_key,
                payload=body.model_dump(mode="json"),
            )
            if not op.is_new:
                return op.response_body
        mailbox_id = uuid.uuid4()
        claimed = (
            await s.execute(
                insert(mailbox_directory)
                .values(address_hash=address_hash(address), tenant_id=tenant_id, mailbox_id=mailbox_id)
                .on_conflict_do_nothing()
                .returning(mailbox_directory.c.mailbox_id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            raise Conflict("This address is already in use")
        row = (
            (
                await s.execute(
                    insert(mailboxes)
                    .values(
                        id=mailbox_id,
                        tenant_id=tenant_id,
                        address=address,
                        display_name=body.display_name,
                        agent_id=body.agent_id,
                        auto_draft=body.auto_draft,
                    )
                    .returning(mailboxes)
                )
            )
            .mappings()
            .one()
        )
        await audit(
            s,
            ctx.actor,
            "email.mailbox.created",
            "mailbox",
            mailbox_id,
            tenant_id=tenant_id,
            auto_draft=body.auto_draft,
        )
        out = MailboxOut.model_validate(dict(row)).model_dump(mode="json")
        if op is not None:
            await idem.succeed(s, op.record_id, status=201, body=out, resource_id=str(mailbox_id))
    return out


@router.patch("/{mailbox_id}", response_model=MailboxOut)
async def patch_mailbox(mailbox_id: uuid.UUID, body: MailboxPatch, request: Request, ctx: Manager) -> Any:
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if not changes:
        raise Unprocessable("No changes requested")
    if changes.get("agent_id") is not None:
        await _check_agent(request, ctx, changes["agent_id"])
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (
            (await s.execute(select(mailboxes).where(mailboxes.c.id == mailbox_id).with_for_update()))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Mailbox not found")
        if row["version"] != body.expected_version:
            raise Conflict("The mailbox changed since you loaded it; reload and retry")
        updated = (
            (
                await s.execute(
                    update(mailboxes)
                    .where(mailboxes.c.id == mailbox_id)
                    .values(**changes, version=mailboxes.c.version + 1, updated_at=func.now())
                    .returning(mailboxes)
                )
            )
            .mappings()
            .one()
        )
        if "status" in changes:
            await s.execute(
                update(mailbox_directory)
                .where(mailbox_directory.c.mailbox_id == mailbox_id, mailbox_directory.c.tenant_id == tenant_id)
                .values(status=changes["status"])
            )
        await audit(
            s,
            ctx.actor,
            "email.mailbox.updated",
            "mailbox",
            mailbox_id,
            tenant_id=tenant_id,
            changed_fields=sorted(changes),
        )
    return updated
