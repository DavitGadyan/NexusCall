"""Inbox threads, thread detail and audited attachment downloads (`emails:inbox:read`)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from email_inbox_service.api.common import db, downloadable, load_messages
from email_inbox_service.api.schemas import DownloadOut, ThreadDetail, ThreadPage
from email_inbox_service.deps import audit
from email_inbox_service.tables import attachments, drafts, threads
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Forbidden, NotFound, ServiceUnavailable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.storage import assert_tenant_key

router = APIRouter(prefix="/api/v1/email/inbox", tags=["inbox"])
Reader = Annotated[AuthContext, Depends(require_permission("emails:inbox:read"))]


@router.get("/threads", response_model=ThreadPage)
async def list_threads(
    request: Request,
    ctx: Reader,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Literal["open", "awaiting_review", "replied", "closed"] | None = None,
    mailbox_id: uuid.UUID | None = None,
    assigned_to_me: bool = False,
) -> Any:
    tenant_id = ctx.require_tenant()
    stmt = select(threads).where(threads.c.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(threads.c.status == status)
    if mailbox_id:
        stmt = stmt.where(threads.c.mailbox_id == mailbox_id)
    if assigned_to_me:
        stmt = stmt.where(threads.c.assigned_to == ctx.principal.sub)
    stmt = apply_keyset(stmt, threads.c.last_message_at, threads.c.id, cursor, limit)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
    page, next_cursor = build_page(rows, limit, created_attr="last_message_at")
    return {"items": page, "next_cursor": next_cursor}


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
async def get_thread(thread_id: uuid.UUID, request: Request, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        thread = (await s.execute(select(threads).where(threads.c.id == thread_id))).mappings().first()
        if thread is None:
            raise NotFound("Thread not found")
        msgs = await load_messages(s, request.app.state.settings, thread_id=thread_id)
        draft_rows = (
            (await s.execute(select(drafts).where(drafts.c.thread_id == thread_id).order_by(drafts.c.created_at)))
            .mappings()
            .all()
        )
    return {**thread, "messages": msgs, "drafts": list(draft_rows)}


@router.get("/attachments/{attachment_id}/download", response_model=DownloadOut)
async def download_attachment(attachment_id: uuid.UUID, request: Request, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    settings = request.app.state.settings
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = (await s.execute(select(attachments).where(attachments.c.id == attachment_id))).mappings().first()
        if row is None:
            raise NotFound("Attachment not found")
        allowed = downloadable(settings, row)
        await audit(
            s,
            ctx.actor,
            "email.attachment.download",
            "email_attachment",
            attachment_id,
            tenant_id=tenant_id,
            outcome="success" if allowed else "denied",
            scan_status=row["scan_status"],
        )
    if not allowed:
        raise Forbidden("This attachment is not available for download (malware scan status)")
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        raise ServiceUnavailable("Object storage is not configured")
    key = assert_tenant_key(tenant_id, row["object_key"])
    ttl = int(settings.attachment_url_ttl_seconds)
    url = await store.presign_get("attachments", key, expires_seconds=ttl, download_filename=row["filename"])
    return {"url": url, "expires_in_seconds": ttl}
