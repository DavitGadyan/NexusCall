"""AI reply draft review queue (build.md §4.2 "Draft review requirements").

* Review actions (`emails:drafts:review`): list, detail, edit (new version), regenerate, reject, assign,
  escalate. None of them can send.
* `POST /approve` (`emails:drafts:approve`, step-up MFA, Idempotency-Key, body `{version}` only) is the ONLY
  way a draft becomes sendable. It re-checks permission and tenant ownership at that moment, requires the
  current version, freezes it, creates the unique send_command, audits, writes the outbox event and enqueues.
* `POST /send` only re-queues a FAILED send of an already approved version. It accepts `{version}` only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from email_inbox_service.api.common import db, load_messages, require_idempotency_key
from email_inbox_service.api.schemas import (
    ApprovalOut,
    AssignIn,
    DraftDetail,
    DraftEdit,
    DraftOut,
    DraftPage,
    EscalateIn,
    RejectIn,
    VersionIn,
)
from email_inbox_service.deps import audit, event, job_context, record_action, request_deps, ui_hint
from email_inbox_service.mime import clean_text, normalize_address, reply_subject
from email_inbox_service.sending import idempotency_key
from email_inbox_service.settings import DRAFT_QUEUE, DRAFT_TASK, SEND_QUEUE, SEND_TASK
from email_inbox_service.tables import (
    draft_actions,
    draft_versions,
    drafts,
    idempotency_keys,
    mailboxes,
    messages,
    send_commands,
    threads,
)
from nexus_core.auth.jwt import Principal
from nexus_core.authz import AuthContext, has_permission, require_permission, require_step_up
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.idempotency import IdempotencyStore
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1/email/drafts", tags=["drafts"])
Reviewer = Annotated[AuthContext, Depends(require_permission("emails:drafts:review"))]
Approver = Annotated[AuthContext, Depends(require_permission("emails:drafts:approve"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
idem = IdempotencyStore(idempotency_keys)

# Statuses from which a human may change the content (an approved, not yet sending version is un-approved).
EDITABLE = ("pending_review", "generation_failed", "approved", "send_failed")
DraftStatusFilter = Literal[
    "queued",
    "generating",
    "pending_review",
    "approved",
    "sending",
    "sent",
    "send_failed",
    "rejected",
    "generation_failed",
]


def _draft_columns() -> list[Any]:
    return [*drafts.c, threads.c.subject]


async def _lock_draft(session: AsyncSession, draft_id: uuid.UUID) -> Any:
    row = (await session.execute(select(drafts).where(drafts.c.id == draft_id).with_for_update())).mappings().first()
    if row is None:
        raise NotFound("Draft not found")
    return row


async def _revoke_pending_send(session: AsyncSession, tenant_id: uuid.UUID, draft: Any, ctx: AuthContext) -> None:
    """Any change after approval cancels the not-yet-sending command: the change needs a new approval."""
    if draft["approved_version"] is None:
        return
    cmd = (
        (
            await session.execute(
                select(send_commands)
                .where(send_commands.c.draft_id == draft["id"], send_commands.c.version == draft["approved_version"])
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if cmd is None:
        return
    if cmd["status"] == "sending":
        raise Conflict("The approved version is being sent right now; wait for the result")
    if cmd["status"] in ("queued", "failed"):
        await session.execute(
            update(send_commands)
            .where(send_commands.c.id == cmd["id"])
            .values(status="canceled", last_error="superseded", updated_at=func.now())
        )
        await record_action(
            session,
            tenant_id=tenant_id,
            draft_id=draft["id"],
            action="send_canceled",
            actor=ctx.actor,
            version=cmd["version"],
            reason="superseded",
        )


def _unapprove() -> dict[str, Any]:
    return {"approved_version": None, "approved_by": None, "approved_at": None, "updated_at": func.now()}


@router.get("", response_model=DraftPage)
async def list_drafts(
    request: Request,
    ctx: Reviewer,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: DraftStatusFilter | None = None,
    escalated: bool | None = None,
    assigned_to_me: bool = False,
) -> Any:
    tenant_id = ctx.require_tenant()
    stmt = (
        select(*_draft_columns())
        .select_from(drafts.join(threads, threads.c.id == drafts.c.thread_id))
        .where(drafts.c.tenant_id == tenant_id)
    )
    if status:
        stmt = stmt.where(drafts.c.status == status)
    if escalated is not None:
        stmt = stmt.where(drafts.c.escalated.is_(escalated))
    if assigned_to_me:
        stmt = stmt.where(drafts.c.assigned_to == ctx.principal.sub)
    stmt = apply_keyset(stmt, drafts.c.created_at, drafts.c.id, cursor, limit)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
    page, next_cursor = build_page(rows, limit)
    return {"items": page, "next_cursor": next_cursor}


async def _detail(session: AsyncSession, settings: Any, draft_id: uuid.UUID) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                select(*_draft_columns())
                .select_from(drafts.join(threads, threads.c.id == drafts.c.thread_id))
                .where(drafts.c.id == draft_id)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Draft not found")
    thread_msgs = await load_messages(session, settings, thread_id=row["thread_id"])
    original = next((m for m in thread_msgs if m["id"] == row["message_id"]), None)
    if original is None:
        original = (await load_messages(session, settings, message_id=row["message_id"]))[0]
    versions = (
        (
            await session.execute(
                select(draft_versions).where(draft_versions.c.draft_id == draft_id).order_by(draft_versions.c.version)
            )
        )
        .mappings()
        .all()
    )
    actions = (
        (
            await session.execute(
                select(draft_actions)
                .where(draft_actions.c.draft_id == draft_id)
                .order_by(draft_actions.c.created_at, draft_actions.c.id)
            )
        )
        .mappings()
        .all()
    )
    commands = (
        (
            await session.execute(
                select(send_commands).where(send_commands.c.draft_id == draft_id).order_by(send_commands.c.version)
            )
        )
        .mappings()
        .all()
    )
    return {
        **row,
        "original_message": original,
        "thread_messages": thread_msgs,
        "versions": [{**v, "ai_generated": v["author_type"] == "ai"} for v in versions],
        "actions": list(actions),
        "send_commands": list(commands),
    }


@router.get("/{draft_id}", response_model=DraftDetail)
async def get_draft(draft_id: uuid.UUID, request: Request, ctx: Reviewer) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        return await _detail(s, request.app.state.settings, draft_id)


@router.put("/{draft_id}", response_model=DraftDetail)
async def edit_draft(draft_id: uuid.UUID, body: DraftEdit, request: Request, ctx: Reviewer) -> Any:
    tenant_id = ctx.require_tenant()
    text = clean_text(body.text, limit=request.app.state.settings.max_draft_chars).strip()
    if not text:
        raise Unprocessable("The reply text is empty")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        draft = await _lock_draft(s, draft_id)
        if draft["status"] not in EDITABLE:
            raise Conflict(f"A draft in status '{draft['status']}' cannot be edited")
        if draft["current_version"] != body.expected_version:
            raise Conflict("The draft changed since you loaded it; reload and retry")
        await _revoke_pending_send(s, tenant_id, draft, ctx)
        version = draft["current_version"] + 1
        await s.execute(
            insert(draft_versions).values(
                tenant_id=tenant_id,
                draft_id=draft_id,
                version=version,
                body_text=text,
                author_type="user",
                author_id=ctx.principal.sub[:128],
            )
        )
        await s.execute(
            update(drafts)
            .where(drafts.c.id == draft_id)
            .values(status="pending_review", current_version=version, last_error=None, **_unapprove())
        )
        await record_action(
            s, tenant_id=tenant_id, draft_id=draft_id, action="edited", actor=ctx.actor, version=version
        )
        await audit(
            s,
            ctx.actor,
            "email.draft.edited",
            "email_draft",
            draft_id,
            tenant_id=tenant_id,
            version=version,
            previous_status=draft["status"],
        )
        detail = await _detail(s, request.app.state.settings, draft_id)
    await ui_hint(
        request_deps(request), tenant_id, "email.draft.updated", "emails:drafts:review", draft_id=str(draft_id)
    )
    return detail


@router.post("/{draft_id}/regenerate", response_model=DraftOut, status_code=202)
async def regenerate_draft(draft_id: uuid.UUID, request: Request, ctx: Reviewer) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        draft = await _lock_draft(s, draft_id)
        if draft["status"] not in EDITABLE:
            raise Conflict(f"A draft in status '{draft['status']}' cannot be regenerated")
        await _revoke_pending_send(s, tenant_id, draft, ctx)
        await s.execute(
            update(drafts)
            .where(drafts.c.id == draft_id)
            .values(status="queued", generation_attempts=0, last_error=None, **_unapprove())
        )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=draft_id,
            action="regenerated",
            actor=ctx.actor,
            version=draft["current_version"],
        )
        await audit(s, ctx.actor, "email.draft.regenerate_requested", "email_draft", draft_id, tenant_id=tenant_id)
        row = await _detail(s, request.app.state.settings, draft_id)
    await request_deps(request).enqueue(
        DRAFT_TASK, DRAFT_QUEUE, job_context(tenant_id, ctx.actor), draft_id=str(draft_id)
    )
    return row


@router.post("/{draft_id}/reject", response_model=DraftOut)
async def reject_draft(draft_id: uuid.UUID, body: RejectIn, request: Request, ctx: Reviewer) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        draft = await _lock_draft(s, draft_id)
        if draft["status"] not in (*EDITABLE, "queued"):
            raise Conflict(f"A draft in status '{draft['status']}' cannot be rejected")
        await _revoke_pending_send(s, tenant_id, draft, ctx)
        await s.execute(update(drafts).where(drafts.c.id == draft_id).values(status="rejected", **_unapprove()))
        await s.execute(
            update(threads).where(threads.c.id == draft["thread_id"]).values(status="open", updated_at=func.now())
        )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=draft_id,
            action="rejected",
            actor=ctx.actor,
            version=draft["current_version"],
            reason=body.reason,
        )
        await audit(
            s,
            ctx.actor,
            "email.draft.rejected",
            "email_draft",
            draft_id,
            tenant_id=tenant_id,
            version=draft["current_version"],
            reason=body.reason,
        )
        await event(
            s,
            "email.reply_draft.rejected",
            tenant_id=tenant_id,
            actor=ctx.actor,
            payload={
                "draft_id": str(draft_id),
                "thread_id": str(draft["thread_id"]),
                "version": draft["current_version"] or None,
            },
        )
        row = await _detail(s, request.app.state.settings, draft_id)
    return row


@router.post("/{draft_id}/assign", response_model=DraftOut)
async def assign_draft(draft_id: uuid.UUID, body: AssignIn, request: Request, ctx: Reviewer) -> Any:
    tenant_id = ctx.require_tenant()
    if body.assignee is not None:
        # The assignee must currently be able to review drafts in THIS tenant (server-side resolution).
        access = await request.app.state.authz_resolver.resolve_tenant(
            Principal(sub=body.assignee, azp=None, is_service=False), tenant_id
        )
        if not access.is_member or not has_permission(access.permissions, "emails:drafts:review"):
            raise Unprocessable("The assignee cannot review drafts in this workspace")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        draft = await _lock_draft(s, draft_id)
        await s.execute(
            update(drafts).where(drafts.c.id == draft_id).values(assigned_to=body.assignee, updated_at=func.now())
        )
        await s.execute(
            update(threads)
            .where(threads.c.id == draft["thread_id"])
            .values(assigned_to=body.assignee, updated_at=func.now())
        )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=draft_id,
            action="assigned",
            actor=ctx.actor,
            details={"assignee": body.assignee},
        )
        await audit(
            s, ctx.actor, "email.draft.assigned", "email_draft", draft_id, tenant_id=tenant_id, assignee=body.assignee
        )
        row = await _detail(s, request.app.state.settings, draft_id)
    return row


@router.post("/{draft_id}/escalate", response_model=DraftOut)
async def escalate_draft(draft_id: uuid.UUID, body: EscalateIn, request: Request, ctx: Reviewer) -> Any:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        draft = await _lock_draft(s, draft_id)
        if draft["status"] in ("sent", "rejected"):
            raise Conflict(f"A draft in status '{draft['status']}' cannot be escalated")
        await s.execute(
            update(drafts)
            .where(drafts.c.id == draft_id)
            .values(escalated=True, escalation_reason=body.reason, updated_at=func.now())
        )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=draft_id,
            action="escalated",
            actor=ctx.actor,
            version=draft["current_version"] or None,
            reason=body.reason,
        )
        await audit(
            s, ctx.actor, "email.draft.escalated", "email_draft", draft_id, tenant_id=tenant_id, reason=body.reason
        )
        await event(
            s,
            "notification.requested",
            tenant_id=tenant_id,
            actor=ctx.actor,
            payload={
                "template": "draft_escalated",
                "permission": "emails:drafts:approve",
                "resource_type": "email_draft",
                "resource_id": str(draft_id),
                "link": f"/app/inbox/drafts/{draft_id}",
                "channel": "in_app",
            },
        )
        row = await _detail(s, request.app.state.settings, draft_id)
    return row


async def _freeze(session: AsyncSession, draft: Any) -> dict[str, Any]:
    """Recipient, sender, subject and threading headers, fixed at approval from trusted stored state."""
    mailbox = (await session.execute(select(mailboxes).where(mailboxes.c.id == draft["mailbox_id"]))).mappings().one()
    if mailbox["status"] != "active":
        raise Conflict("The mailbox is disabled")
    inbound = (await session.execute(select(messages).where(messages.c.id == draft["message_id"]))).mappings().one()
    thread = (await session.execute(select(threads).where(threads.c.id == draft["thread_id"]))).mappings().one()
    to_address = normalize_address(inbound["from_address"])
    if to_address is None or "loop" in inbound["auto_flags"] or to_address == mailbox["address"]:
        raise Conflict("The original sender address cannot receive a reply")
    rfc_id = inbound["rfc_message_id"]
    real_id = rfc_id if rfc_id and not rfc_id.endswith("@inbound.nexus.invalid>") else None
    refs = [r for r in inbound["rfc_references"] if r][-19:]
    if real_id:
        refs = [*refs, real_id]
    return {
        "from_address": mailbox["address"],
        "to_address": to_address,
        "subject": reply_subject(thread["subject"] or inbound["subject"]),
        "in_reply_to": real_id,
        "rfc_references": refs,
    }


@router.post("/{draft_id}/approve", response_model=ApprovalOut)
async def approve_draft(
    draft_id: uuid.UUID,
    body: VersionIn,
    request: Request,
    ctx: Approver,
    _step_up: StepUp,
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    tenant_id = ctx.require_tenant()
    key = require_idempotency_key(idempotency_key_header)
    approver = ctx.principal.sub
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem.begin(
            s, tenant_id=tenant_id, operation=f"draft.approve:{draft_id}", key=key, payload=body.model_dump()
        )
        if not op.is_new:
            return op.response_body
        draft = await _lock_draft(s, draft_id)  # tenant ownership: RLS + explicit lookup inside the tenant session
        if draft["status"] != "pending_review":
            raise Conflict(f"A draft in status '{draft['status']}' cannot be approved")
        if draft["current_version"] < 1 or body.version != draft["current_version"]:
            raise Conflict("Only the current version can be approved; reload the draft")
        frozen = await _freeze(s, draft)
        now = datetime.now(UTC)
        values = {
            **frozen,
            "status": "queued",
            "approved_by": approver,
            "approved_at": now,
            "attempts": 0,
            "last_error": None,
            "updated_at": func.now(),
        }
        command_id = (
            await s.execute(
                insert(send_commands)
                .values(
                    tenant_id=tenant_id,
                    draft_id=draft_id,
                    version=body.version,
                    idempotency_key=idempotency_key(draft_id, body.version),
                    **values,
                )
                .on_conflict_do_update(
                    constraint="uq_send_commands_draft_version",
                    set_=values,
                    where=send_commands.c.status == "blocked",  # re-approval after a blocked send only
                )
                .returning(send_commands.c.id)
            )
        ).scalar_one_or_none()
        if command_id is None:
            raise Conflict("This version has already been approved")
        await s.execute(
            update(drafts)
            .where(drafts.c.id == draft_id)
            .values(
                status="approved",
                approved_version=body.version,
                approved_by=approver,
                approved_at=now,
                last_error=None,
                updated_at=func.now(),
            )
        )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=draft_id,
            action="approved",
            actor=ctx.actor,
            version=body.version,
            details={"send_command_id": str(command_id)},
        )
        await audit(
            s,
            ctx.actor,
            "email.draft.approved",
            "email_draft",
            draft_id,
            tenant_id=tenant_id,
            version=body.version,
            send_command_id=str(command_id),
            escalated=draft["escalated"],
        )
        await event(
            s,
            "email.reply_draft.approved",
            tenant_id=tenant_id,
            actor=ctx.actor,
            payload={"draft_id": str(draft_id), "thread_id": str(draft["thread_id"]), "version": body.version},
        )
        out = {
            "draft_id": str(draft_id),
            "version": body.version,
            "status": "approved",
            "send_command_id": str(command_id),
        }
        await idem.succeed(s, op.record_id, status=200, body=out, resource_id=str(command_id))
    await request_deps(request).enqueue(
        SEND_TASK, SEND_QUEUE, job_context(tenant_id, ctx.actor), command_id=str(command_id)
    )
    return out


@router.post("/{draft_id}/send", response_model=ApprovalOut, status_code=202)
async def retry_send(
    draft_id: uuid.UUID,
    body: VersionIn,
    request: Request,
    ctx: Approver,
    _step_up: StepUp,
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """Re-queue a FAILED send of the already approved version. Never sends anything unapproved."""
    tenant_id = ctx.require_tenant()
    key = require_idempotency_key(idempotency_key_header)
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem.begin(
            s, tenant_id=tenant_id, operation=f"draft.send:{draft_id}", key=key, payload=body.model_dump()
        )
        if not op.is_new:
            return op.response_body
        draft = await _lock_draft(s, draft_id)
        if draft["status"] != "send_failed" or draft["approved_version"] != body.version:
            raise Conflict("Only a failed send of the approved version can be retried")
        cmd = (
            (
                await s.execute(
                    select(send_commands)
                    .where(send_commands.c.draft_id == draft_id, send_commands.c.version == body.version)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if cmd is None or cmd["status"] != "failed":
            raise Conflict("There is no failed send to retry for this version")
        await s.execute(
            update(send_commands)
            .where(send_commands.c.id == cmd["id"])
            .values(status="queued", attempts=0, last_error=None, updated_at=func.now())
        )
        await s.execute(
            update(drafts)
            .where(drafts.c.id == draft_id)
            .values(status="approved", last_error=None, updated_at=func.now())
        )
        await record_action(
            s, tenant_id=tenant_id, draft_id=draft_id, action="send_retry", actor=ctx.actor, version=body.version
        )
        await audit(
            s, ctx.actor, "email.reply.send_retry", "email_draft", draft_id, tenant_id=tenant_id, version=body.version
        )
        out = {
            "draft_id": str(draft_id),
            "version": body.version,
            "status": "approved",
            "send_command_id": str(cmd["id"]),
        }
        await idem.succeed(s, op.record_id, status=202, body=out, resource_id=str(cmd["id"]))
    await request_deps(request).enqueue(
        SEND_TASK, SEND_QUEUE, job_context(tenant_id, ctx.actor), command_id=str(cmd["id"])
    )
    return out
