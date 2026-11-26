"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): a month of inbound customer email to the
Acme support mailbox with AI reply drafts in every human-review state: waiting for review (one escalated, two
assigned, one already edited), approved and sent, rejected, and an unsubscribe request closed without reply.

Idempotent (fixed ids, ON CONFLICT DO NOTHING; append-only draft history is written only with its draft). Runs as
`inbox_app`, so RLS applies. No draft is queued or generating and every send command is already `sent`, so
neither the drafting worker nor the sender acts on them: seeding calls no model and sends nothing.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from email_inbox_service.sending import idempotency_key
from email_inbox_service.tables import draft_actions, draft_versions, drafts, messages, send_commands, threads
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devshowcase import MAILBOX_ADDRESS, MAILBOX_ID, TENANT, InboxMessage, Scenario, Thread, sid

SEED_ACTOR = Actor("system", "dev-showcase")


async def seed(db: Database, sc: Scenario) -> None:
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        for th in sc.threads:
            first = th.messages[0]
            await s.execute(
                insert(threads)
                .values(
                    id=th.id,
                    tenant_id=TENANT.id,
                    mailbox_id=MAILBOX_ID,
                    subject=th.subject,
                    contact_id=th.contact.id,
                    status=th.status,
                    priority=th.priority,
                    intent=th.intent,
                    assigned_to=th.assigned_to,
                    message_count=len(th.messages),
                    last_message_at=th.last_message_at,
                    version=len(th.draft_actions) + 1,
                    created_at=first.received_at,
                    updated_at=th.draft_updated_at or first.received_at,
                )
                .on_conflict_do_nothing()
            )
            for m in th.messages[:1]:  # the draft references the inbound message, so it goes first
                await s.execute(insert(messages).values(**_message(th, m)).on_conflict_do_nothing())
            if th.draft_id is not None:
                created = (
                    await s.execute(
                        insert(drafts)
                        .values(
                            id=th.draft_id,
                            tenant_id=TENANT.id,
                            thread_id=th.id,
                            message_id=first.id,
                            mailbox_id=MAILBOX_ID,
                            status=th.draft_status,
                            current_version=max(v.version for v in th.draft_versions),
                            approved_version=th.approved_version,
                            approved_by=th.approved_by,
                            approved_at=th.approved_at,
                            assigned_to=th.assigned_to,
                            escalated=th.escalated,
                            escalation_reason=th.escalation_reason,
                            intent=th.intent,
                            priority=th.priority,
                            sensitive=th.sensitive,
                            summary=th.draft_summary,
                            generation_attempts=1,
                            created_at=th.draft_created_at,
                            updated_at=th.draft_updated_at,
                        )
                        .on_conflict_do_nothing()
                        .returning(drafts.c.id)
                    )
                ).scalar_one_or_none()
                if created is not None:
                    await _history(s, th)
            for m in th.messages[1:]:
                await s.execute(insert(messages).values(**_message(th, m)).on_conflict_do_nothing())


def _message(t: Thread, msg: InboxMessage) -> dict[str, object]:
    return {
        "id": msg.id,
        "tenant_id": TENANT.id,
        "thread_id": t.id,
        "mailbox_id": MAILBOX_ID,
        "direction": msg.direction,
        "rfc_message_id": msg.rfc_message_id,
        "in_reply_to": msg.in_reply_to,
        "rfc_references": [msg.in_reply_to] if msg.in_reply_to else [],
        "from_address": msg.from_address,
        "from_name": msg.from_name,
        "to_addresses": [msg.to_address],
        "subject": msg.subject,
        "text_body": msg.text,
        "size_bytes": len(msg.text.encode()) + 900,
        "contact_id": t.contact.id,
        "draft_id": t.draft_id if msg.direction == "outbound" else None,
        "draft_version": msg.draft_version,
        "outbound_message_id": str(t.reply_email_id) if msg.direction == "outbound" and t.reply_email_id else None,
        "received_at": msg.received_at,
        "created_at": msg.received_at,
    }


async def _history(s: AsyncSession, th: Thread) -> None:
    for v in th.draft_versions:
        ai = v.author_type == "ai"
        await s.execute(
            insert(draft_versions).values(
                id=sid("draft_version", th.n * 10 + v.version),
                tenant_id=TENANT.id,
                draft_id=th.draft_id,
                version=v.version,
                body_text=v.body,
                author_type=v.author_type,
                author_id=v.author_id,
                model="claude-sonnet-5-5" if ai else None,
                provider="anthropic" if ai else None,
                prompt_version=1 if ai else None,
                citations=v.citations,
                quality=v.quality,
                created_at=v.created_at,
            )
        )
    for a in th.draft_actions:
        await s.execute(
            insert(draft_actions).values(
                id=a.id,
                tenant_id=TENANT.id,
                draft_id=th.draft_id,
                action=a.action,
                version=a.version,
                actor_type=a.actor_type,
                actor_id=a.actor_id,
                reason=a.reason,
                details=a.details,
                created_at=a.created_at,
            )
        )
    if th.draft_status == "sent" and th.approved_version and th.approved_at and th.draft_id:
        first = th.messages[0]
        await s.execute(
            insert(send_commands).values(
                id=sid("send_command", th.n),
                tenant_id=TENANT.id,
                draft_id=th.draft_id,
                version=th.approved_version,
                status="sent",
                approved_by=th.approved_by,
                approved_at=th.approved_at,
                from_address=MAILBOX_ADDRESS,
                to_address=first.from_address,
                subject=f"Re: {th.subject}",
                in_reply_to=first.rfc_message_id,
                rfc_references=[first.rfc_message_id] if first.rfc_message_id else [],
                idempotency_key=idempotency_key(th.draft_id, th.approved_version),
                attempts=1,
                outbound_message_id=str(th.reply_email_id) if th.reply_email_id else None,
                provider_state="delivered",
                sent_at=th.sent_at,
                created_at=th.approved_at,
                updated_at=(th.sent_at or th.approved_at) + timedelta(seconds=1),
            )
        )
