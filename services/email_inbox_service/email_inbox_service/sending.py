"""`emails.send_approved`: send exactly one frozen, human-approved draft version.

There is NO other path to email-service from this service. Before anything leaves, the task requires, from
persisted server-side state only:
1. a `queued` send_command for (draft, version), unique in the database;
2. the draft still `approved` at exactly that version, by the same approver;
3. an append-only `approved` action by a human user for that version (approval evidence);
and then re-checks, at send time, through the owning services:
4. the approver still holds `emails:drafts:approve` in this tenant (tenant-service authz/resolve, uncached);
5. the tenant's `effective_emails_enabled` (platform + tenant kill switches);
6. the recipient is not suppressed (crm-service).
The email-service call carries the idempotency key `draft:{id}:v{n}`; a retry after an ambiguous failure
replays the same key and payload, so email-service reconciles instead of sending twice.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_inbox_service.deps import Deps, audit, event, record_action, ui_hint
from email_inbox_service.inbound import sender_suppressed
from email_inbox_service.tables import draft_actions, draft_versions, drafts, messages, send_commands, threads
from nexus_core.authz import has_permission
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.context import Actor

log = logging.getLogger(__name__)
SEND_ACTOR = Actor("system", "email-sender")
APPROVE_PERMISSION = "emails:drafts:approve"
Outcome = Literal["sent", "skipped", "canceled", "blocked", "failed"]


def idempotency_key(draft_id: uuid.UUID, version: int) -> str:
    return f"draft:{draft_id}:v{version}"


async def _close(
    deps: Deps,
    tenant_id: uuid.UUID,
    cmd: Any,
    *,
    status: Literal["blocked", "failed", "canceled"],
    code: str,
) -> Outcome:
    """Terminal (for this command) outcome without sending. `blocked` needs a new approval; `failed` can be
    retried by an approver via POST /drafts/{id}/send; `canceled` means the approval no longer applies."""
    action = {"blocked": "send_blocked", "failed": "send_failed", "canceled": "send_canceled"}[status]
    async with deps.db.tenant_session(tenant_id, SEND_ACTOR) as s:
        await s.execute(
            update(send_commands)
            .where(send_commands.c.id == cmd["id"])
            .values(status=status, last_error=code, updated_at=func.now())
        )
        if status == "blocked":
            await s.execute(
                update(drafts)
                .where(
                    drafts.c.id == cmd["draft_id"],
                    drafts.c.approved_version == cmd["version"],
                    drafts.c.status.in_(("approved", "sending")),
                )
                .values(
                    status="pending_review",
                    approved_version=None,
                    approved_by=None,
                    approved_at=None,
                    last_error=code,
                    updated_at=func.now(),
                )
            )
        elif status == "failed":
            await s.execute(
                update(drafts)
                .where(
                    drafts.c.id == cmd["draft_id"],
                    drafts.c.approved_version == cmd["version"],
                    drafts.c.status.in_(("approved", "sending")),
                )
                .values(status="send_failed", last_error=code, updated_at=func.now())
            )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=cmd["draft_id"],
            action=action,
            actor=SEND_ACTOR,
            version=cmd["version"],
            reason=code,
        )
        await audit(
            s,
            SEND_ACTOR,
            "email.reply.send_" + status,
            "email_draft",
            cmd["draft_id"],
            tenant_id=tenant_id,
            outcome="denied" if status == "blocked" else "failure",
            version=cmd["version"],
            reason=code,
        )
    await ui_hint(deps, tenant_id, "email.draft.updated", "emails:drafts:review", draft_id=str(cmd["draft_id"]))
    return status


async def _release(deps: Deps, tenant_id: uuid.UUID, cmd: Any, code: str) -> None:
    """Transient problem: hand the command back to the queue (the retry reuses the same idempotency key)."""
    async with deps.db.tenant_session(tenant_id, SEND_ACTOR) as s:
        await s.execute(
            update(send_commands)
            .where(send_commands.c.id == cmd["id"], send_commands.c.status == "sending")
            .values(status="queued", last_error=code, updated_at=func.now())
        )
        await s.execute(
            update(drafts)
            .where(drafts.c.id == cmd["draft_id"], drafts.c.status == "sending")
            .values(status="approved", updated_at=func.now())
        )


async def _claim(deps: Deps, tenant_id: uuid.UUID, command_id: uuid.UUID) -> tuple[Any, str | None]:
    """Lock the command and its draft; verify the persisted approval. -> (command, problem code or None)."""
    async with deps.db.tenant_session(tenant_id, SEND_ACTOR) as s:
        cmd = (
            (await s.execute(select(send_commands).where(send_commands.c.id == command_id).with_for_update()))
            .mappings()
            .first()
        )
        if cmd is None or cmd["status"] != "queued":
            return cmd, "not_queued"
        draft = (
            (await s.execute(select(drafts).where(drafts.c.id == cmd["draft_id"]).with_for_update())).mappings().one()
        )
        if draft["status"] != "approved" or draft["approved_version"] != cmd["version"]:
            return cmd, "not_approved"
        if draft["approved_by"] != cmd["approved_by"]:
            return cmd, "approval_mismatch"
        evidence = (
            await s.execute(
                select(draft_actions.c.id).where(
                    draft_actions.c.draft_id == cmd["draft_id"],
                    draft_actions.c.action == "approved",
                    draft_actions.c.version == cmd["version"],
                    draft_actions.c.actor_type == "user",
                    draft_actions.c.actor_id == cmd["approved_by"],
                )
            )
        ).first()
        if evidence is None:
            return cmd, "approval_missing"
        if cmd["attempts"] >= deps.settings.send_max_attempts:
            return cmd, "too_many_attempts"
        await s.execute(
            update(send_commands)
            .where(send_commands.c.id == command_id)
            .values(status="sending", attempts=send_commands.c.attempts + 1, updated_at=func.now())
        )
        await s.execute(
            update(drafts).where(drafts.c.id == cmd["draft_id"]).values(status="sending", updated_at=func.now())
        )
    return cmd, None


async def approver_still_authorized(deps: Deps, tenant_id: uuid.UUID, approver: str) -> bool | None:
    """Fresh (uncached) decision from tenant-service. None when tenant-service is unavailable."""
    try:
        res = await deps.client("tenant-service").post_json(
            "/internal/v1/authz/resolve",
            {"sub": approver, "tenant_id": str(tenant_id)},
            tenant_id=tenant_id,
            actor=SEND_ACTOR,
        )
    except UpstreamUnavailable:
        return None
    except UpstreamError as exc:
        return None if exc.upstream_status >= 500 else False
    if not isinstance(res, dict) or not res.get("is_member") or res.get("tenant_status") != "active":
        return False
    return has_permission(frozenset(str(p) for p in res.get("permissions", [])), APPROVE_PERMISSION)


async def _emails_enabled(deps: Deps, tenant_id: uuid.UUID) -> bool | None:
    try:
        comm = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "communication")
    except (UpstreamUnavailable, UpstreamError):
        return None
    return comm.get("effective_emails_enabled") is True


async def send_approved(deps: Deps, ctx: JobContext, command_id: uuid.UUID) -> Outcome:  # noqa: PLR0912, PLR0915
    tenant_id = ctx.tenant_id
    cmd, problem = await _claim(deps, tenant_id, command_id)
    if problem == "not_queued":
        return "skipped"
    if problem == "not_approved":
        return await _close(deps, tenant_id, cmd, status="canceled", code="not_approved")
    if problem == "too_many_attempts":
        return await _close(deps, tenant_id, cmd, status="failed", code=problem)
    if problem is not None:
        return await _close(deps, tenant_id, cmd, status="blocked", code=problem)

    authorized = await approver_still_authorized(deps, tenant_id, cmd["approved_by"])
    if authorized is None:
        await _release(deps, tenant_id, cmd, "authz_unavailable")
        raise TransientFailure("tenant-service unavailable")
    if not authorized:
        return await _close(deps, tenant_id, cmd, status="blocked", code="approver_not_authorized")
    enabled = await _emails_enabled(deps, tenant_id)
    if enabled is None:
        await _release(deps, tenant_id, cmd, "settings_unavailable")
        raise TransientFailure("tenant settings unavailable")
    if not enabled:
        return await _close(deps, tenant_id, cmd, status="failed", code="emails_disabled")
    suppressed = await sender_suppressed(deps, tenant_id, cmd["to_address"], SEND_ACTOR)
    if suppressed is None:
        await _release(deps, tenant_id, cmd, "crm_unavailable")
        raise TransientFailure("suppression check unavailable")
    if suppressed:
        return await _close(deps, tenant_id, cmd, status="blocked", code="recipient_suppressed")

    async with deps.db.tenant_session(tenant_id, SEND_ACTOR) as s:
        frozen = (
            await s.execute(
                select(draft_versions.c.body_text).where(
                    draft_versions.c.draft_id == cmd["draft_id"], draft_versions.c.version == cmd["version"]
                )
            )
        ).scalar_one()
        draft = (await s.execute(select(drafts).where(drafts.c.id == cmd["draft_id"]))).mappings().one()
    headers: dict[str, str] = {}
    if cmd["in_reply_to"]:
        headers["In-Reply-To"] = cmd["in_reply_to"]
    if cmd["rfc_references"]:
        headers["References"] = " ".join(cmd["rfc_references"])
    body = {
        "category": "reply",
        "from_address": cmd["from_address"],
        "to": cmd["to_address"],
        "subject": cmd["subject"],
        "text": frozen,
        "html": None,
        "headers": headers,
        "source": {"type": "email_draft", "id": str(cmd["draft_id"]), "version": cmd["version"]},
    }
    try:
        res = await deps.client("email-service").post_json(
            "/internal/v1/send",
            body,
            tenant_id=tenant_id,
            actor=SEND_ACTOR,
            idempotency_key=cmd["idempotency_key"],
            expect=(200, 201, 202),
        )
    except UpstreamUnavailable as exc:
        await _release(deps, tenant_id, cmd, "send_unavailable")
        raise TransientFailure("email-service unavailable") from exc
    except UpstreamError as exc:
        if exc.upstream_status >= 500 or exc.upstream_status in (408, 409, 429):
            await _release(deps, tenant_id, cmd, f"send_http_{exc.upstream_status}")
            raise TransientFailure("email-service failed transiently") from exc
        return await _close(deps, tenant_id, cmd, status="failed", code=f"send_rejected_{exc.upstream_status}")

    outbound_id = str((res or {}).get("message_id", ""))[:128] or None
    state = str((res or {}).get("state", "queued"))[:32]
    now = datetime.now(UTC)
    async with deps.db.tenant_session(tenant_id, SEND_ACTOR) as s:
        await s.execute(
            update(send_commands)
            .where(send_commands.c.id == cmd["id"])
            .values(
                status="sent",
                outbound_message_id=outbound_id,
                provider_state=state,
                sent_at=now,
                last_error=None,
                updated_at=func.now(),
            )
        )
        await s.execute(
            update(drafts).where(drafts.c.id == cmd["draft_id"]).values(status="sent", updated_at=func.now())
        )
        await s.execute(
            insert(messages).values(
                tenant_id=tenant_id,
                thread_id=draft["thread_id"],
                mailbox_id=draft["mailbox_id"],
                direction="outbound",
                in_reply_to=cmd["in_reply_to"],
                rfc_references=cmd["rfc_references"],
                from_address=cmd["from_address"],
                to_addresses=[cmd["to_address"]],
                subject=cmd["subject"],
                text_body=frozen,
                draft_id=cmd["draft_id"],
                draft_version=cmd["version"],
                outbound_message_id=outbound_id,
                received_at=now,
            )
        )
        await s.execute(
            update(threads)
            .where(threads.c.id == draft["thread_id"])
            .values(
                status="replied", message_count=threads.c.message_count + 1, last_message_at=now, updated_at=func.now()
            )
        )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=cmd["draft_id"],
            action="sent",
            actor=SEND_ACTOR,
            version=cmd["version"],
            details={"outbound_message_id": outbound_id, "state": state},
        )
        await audit(
            s,
            SEND_ACTOR,
            "email.reply.sent",
            "email_draft",
            cmd["draft_id"],
            tenant_id=tenant_id,
            version=cmd["version"],
            approved_by=cmd["approved_by"],
            outbound_message_id=outbound_id,
        )
        await event(
            s,
            "email.reply.sent",
            tenant_id=tenant_id,
            actor=SEND_ACTOR,
            payload={"draft_id": str(cmd["draft_id"]), "thread_id": str(draft["thread_id"]), "version": cmd["version"]},
        )
    await ui_hint(deps, tenant_id, "email.draft.updated", "emails:drafts:review", draft_id=str(cmd["draft_id"]))
    return "sent"
