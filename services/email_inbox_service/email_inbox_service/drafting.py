"""`emails.ai_draft`: build untrusted context -> ai-agent drafting run -> append-only draft version.

The result is ONLY ever a draft. Nothing here can send, approve or call tools: ai-agent-service runs the
drafting with no tools, and this module writes `pending_review` drafts only. Sending requires a persisted
human approval (`api/drafts.approve_draft`) and is re-authorized at send time (`sending.send_approved`).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_inbox_service.deps import Deps, audit, event, record_action, ui_hint
from email_inbox_service.inbound import sender_suppressed
from email_inbox_service.mime import clean_text
from email_inbox_service.tables import attachments, draft_versions, drafts, mailboxes, messages, threads
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.clients import UpstreamError, UpstreamUnavailable
from nexus_core.context import Actor

log = logging.getLogger(__name__)
DRAFT_ACTOR = Actor("system", "email-drafting")
# build.md §11 / brief: complaints, legal and privacy requests and unsubscribe requests go to a human lead.
ESCALATE_INTENTS = frozenset({"complaint", "legal", "privacy_request", "unsubscribe", "billing_dispute"})
ESCALATE_FLAGS = frozenset({"sensitive", "injection_suspected", "needs_human", "output_blocked"})
PRIORITIES = ("low", "normal", "high", "urgent")
_INTENT = re.compile(r"^[a-z_]{1,32}$")
_FLAG = re.compile(r"^[a-z_]{1,40}$")


class _Citation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str = Field(max_length=300)


class _Quality(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confidence: float = Field(ge=0, le=1)
    flags: list[str] = Field(default_factory=list, max_length=50)


class DraftingResult(BaseModel):
    """Strict view of ai-agent `POST /internal/v1/drafting/runs` (BUILD_CONVENTIONS §5). Model output is
    untrusted: unexpected types fail validation, and text fields are cleaned before storage."""

    model_config = ConfigDict(extra="ignore")
    run_id: uuid.UUID
    intent: str = Field(max_length=64)
    priority: str = Field(max_length=16)
    sensitive: bool
    summary: str = Field(max_length=20_000)
    draft_text: str = Field(max_length=100_000)
    citations: list[_Citation] = Field(default_factory=list, max_length=20)
    quality: _Quality
    model: str = Field(max_length=100)
    provider: str = Field(max_length=32)
    prompt_version: int


Outcome = Literal["generated", "skipped", "failed"]


async def _fail(deps: Deps, tenant_id: uuid.UUID, draft: Any, code: str, actor: Actor) -> Outcome:
    async with deps.db.tenant_session(tenant_id, actor) as s:
        await s.execute(
            update(drafts)
            .where(drafts.c.id == draft["id"], drafts.c.status == "generating")
            .values(status="generation_failed", last_error=code[:64], updated_at=func.now())
        )
        await s.execute(
            update(threads).where(threads.c.id == draft["thread_id"]).values(status="open", updated_at=func.now())
        )
        await record_action(
            s, tenant_id=tenant_id, draft_id=draft["id"], action="generation_failed", actor=actor, reason=code[:64]
        )
        await audit(
            s,
            actor,
            "email.draft.generation_failed",
            "email_draft",
            draft["id"],
            tenant_id=tenant_id,
            outcome="failure",
            reason=code[:64],
        )
    await ui_hint(deps, tenant_id, "email.draft.updated", "emails:drafts:review", draft_id=str(draft["id"]))
    return "failed"


async def _requeue(deps: Deps, tenant_id: uuid.UUID, draft_id: uuid.UUID, actor: Actor, code: str) -> None:
    async with deps.db.tenant_session(tenant_id, actor) as s:
        await s.execute(
            update(drafts)
            .where(drafts.c.id == draft_id, drafts.c.status == "generating")
            .values(status="queued", last_error=code, updated_at=func.now())
        )


def context_blocks(settings: Any, history: list[Any], names: list[str]) -> list[dict[str, str]]:
    """Every block is UNTRUSTED data from outside the platform. Nothing here is an instruction."""
    blocks = []
    for row in history:
        body = clean_text(row["text_body"], limit=settings.draft_context_chars_per_message)
        who = "customer" if row["direction"] == "inbound" else "our previous reply"
        blocks.append(
            {
                "text": (
                    f"[{who}] From: {row['from_name']} <{row['from_address']}>\n" f"Subject: {row['subject']}\n\n{body}"
                ),
                "source": "email",
                "trust": "untrusted",
            }
        )
    if names:
        listing = "\n".join(f"- {clean_text(n, limit=120)}" for n in names[:20])
        blocks.append(
            {"text": f"Attachment file names (not opened):\n{listing}", "source": "attachment", "trust": "untrusted"}
        )
    return blocks


async def generate_draft(deps: Deps, ctx: JobContext, draft_id: uuid.UUID) -> Outcome:  # noqa: PLR0915
    tenant_id, actor, settings = ctx.tenant_id, DRAFT_ACTOR, deps.settings
    async with deps.db.tenant_session(tenant_id, actor) as s:
        draft = (
            (
                await s.execute(
                    update(drafts)
                    .where(drafts.c.id == draft_id, drafts.c.status == "queued")
                    .values(
                        status="generating", generation_attempts=drafts.c.generation_attempts + 1, updated_at=func.now()
                    )
                    .returning(drafts)
                )
            )
            .mappings()
            .first()
        )
        if draft is None:
            return "skipped"
        mailbox = (await s.execute(select(mailboxes).where(mailboxes.c.id == draft["mailbox_id"]))).mappings().one()
        target = (await s.execute(select(messages).where(messages.c.id == draft["message_id"]))).mappings().one()
        history = list(
            (
                await s.execute(
                    select(messages)
                    .where(messages.c.thread_id == draft["thread_id"], messages.c.received_at <= target["received_at"])
                    .order_by(messages.c.received_at.desc(), messages.c.id.desc())
                    .limit(settings.draft_context_messages)
                )
            )
            .mappings()
            .all()
        )[::-1]
        names = list(
            (await s.execute(select(attachments.c.filename).where(attachments.c.message_id == target["id"])))
            .scalars()
            .all()
        )
    if draft["generation_attempts"] > settings.draft_generation_max_attempts:
        return await _fail(deps, tenant_id, draft, "too_many_attempts", actor)
    suppressed = await sender_suppressed(deps, tenant_id, target["from_address"], actor)
    if suppressed is None:
        await _requeue(deps, tenant_id, draft_id, actor, "crm_unavailable")
        raise TransientFailure("suppression check unavailable")
    if suppressed:
        return await _fail(deps, tenant_id, draft, "sender_suppressed", actor)

    body = {
        "thread_id": str(draft["thread_id"]),
        "message_id": str(draft["message_id"]),
        "agent_id": str(mailbox["agent_id"]) if mailbox["agent_id"] else None,
        "context_blocks": context_blocks(settings, history, names),
    }
    try:
        raw = await deps.client("ai-agent-service").post_json(
            "/internal/v1/drafting/runs", body, tenant_id=tenant_id, actor=actor, timeout_seconds=150.0
        )
    except UpstreamUnavailable as exc:
        await _requeue(deps, tenant_id, draft_id, actor, "drafting_unavailable")
        raise TransientFailure("drafting unavailable") from exc
    except UpstreamError as exc:
        if exc.upstream_status >= 500 or exc.upstream_status in (408, 409, 429):
            await _requeue(deps, tenant_id, draft_id, actor, f"drafting_http_{exc.upstream_status}")
            raise TransientFailure("drafting failed transiently") from exc
        code = exc.upstream_code if exc.upstream_code and re.match(r"^[a-z_]{1,40}$", exc.upstream_code) else None
        return await _fail(deps, tenant_id, draft, code or f"drafting_http_{exc.upstream_status}", actor)
    try:
        result = DraftingResult.model_validate(raw)
    except ValidationError:
        return await _fail(deps, tenant_id, draft, "invalid_draft_output", actor)
    text = clean_text(result.draft_text, limit=settings.max_draft_chars).strip()
    if not text:
        return await _fail(deps, tenant_id, draft, "empty_draft", actor)
    intent = result.intent if _INTENT.match(result.intent) else "other"
    priority = result.priority if result.priority in PRIORITIES else "normal"
    flags = sorted({f for f in result.quality.flags if isinstance(f, str) and _FLAG.match(f)})
    escalate_reason = (
        f"intent_{intent}"
        if intent in ESCALATE_INTENTS
        else "sensitive"
        if result.sensitive
        else next((f"flag_{f}" for f in flags if f in ESCALATE_FLAGS), None)
    )
    citations = [c.model_dump(mode="json") for c in result.citations]
    async with deps.db.tenant_session(tenant_id, actor) as s:
        current = (
            await s.execute(
                select(drafts.c.current_version, drafts.c.status).where(drafts.c.id == draft_id).with_for_update()
            )
        ).one()
        if current.status != "generating":  # rejected or edited meanwhile: drop the result
            return "skipped"
        version = current.current_version + 1
        await s.execute(
            insert(draft_versions).values(
                tenant_id=tenant_id,
                draft_id=draft_id,
                version=version,
                body_text=text,
                author_type="ai",
                author_id="ai-agent-service",
                model=result.model[:100],
                provider=result.provider[:32],
                prompt_version=result.prompt_version,
                run_id=result.run_id,
                citations=citations,
                quality={"confidence": result.quality.confidence, "flags": flags},
            )
        )
        values: dict[str, Any] = {
            "status": "pending_review",
            "current_version": version,
            "intent": intent,
            "priority": priority,
            "sensitive": bool(result.sensitive or "sensitive" in flags),
            "summary": clean_text(result.summary, limit=2000),
            "last_error": None,
            "updated_at": func.now(),
        }
        if escalate_reason:
            values |= {"escalated": True, "escalation_reason": escalate_reason}
        await s.execute(update(drafts).where(drafts.c.id == draft_id).values(**values))
        await s.execute(
            update(threads)
            .where(threads.c.id == draft["thread_id"])
            .values(status="awaiting_review", intent=intent, priority=priority, updated_at=func.now())
        )
        await record_action(
            s,
            tenant_id=tenant_id,
            draft_id=draft_id,
            action="generated",
            actor=actor,
            version=version,
            details={"run_id": str(result.run_id), "flags": flags, "citations": len(citations)},
        )
        await audit(
            s,
            actor,
            "email.draft.generated",
            "email_draft",
            draft_id,
            tenant_id=tenant_id,
            version=version,
            model=result.model[:100],
            prompt_version=result.prompt_version,
            flags=flags,
        )
        await event(
            s,
            "email.reply_draft.generated",
            tenant_id=tenant_id,
            actor=actor,
            payload={"draft_id": str(draft_id), "thread_id": str(draft["thread_id"]), "version": version},
        )
        if escalate_reason:
            await record_action(
                s,
                tenant_id=tenant_id,
                draft_id=draft_id,
                action="escalated",
                actor=actor,
                version=version,
                reason=escalate_reason[:64],
            )
            await event(
                s,
                "notification.requested",
                tenant_id=tenant_id,
                actor=actor,
                payload={
                    "template": "draft_escalated",
                    "permission": "emails:drafts:approve",
                    "resource_type": "email_draft",
                    "resource_id": str(draft_id),
                    "link": f"/app/inbox/drafts/{draft_id}",
                    "channel": "in_app",
                },
            )
    await ui_hint(deps, tenant_id, "email.draft.updated", "emails:drafts:review", draft_id=str(draft_id))
    return "generated"
