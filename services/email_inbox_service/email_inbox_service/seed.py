"""LOCAL DEVELOPMENT demo data: one inbound mailbox per demo tenant, one inbound thread and one AI draft
awaiting human review. Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as `inbox_app`, so RLS applies.

Addresses are under the reserved `.example.test` domain. The draft is pending review: nothing is sent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from email_inbox_service.mime import address_hash
from email_inbox_service.tables import (
    draft_actions,
    draft_versions,
    drafts,
    mailbox_directory,
    mailboxes,
    messages,
    threads,
)
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, GLOBEX, demo_id

SEED_ACTOR = Actor("system", "dev-seed")
# (tenant, id offset, mailbox address)
TENANTS = ((ACME, 0, "support@acme.example.test"), (GLOBEX, 100, "frontdesk@globex.example.test"))


async def seed(db: Database) -> None:
    received = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)
    for tenant, offset, address in TENANTS:
        mailbox_id = demo_id("mailbox", 1 + offset)
        thread_id = demo_id("inbox-thread", 1 + offset)
        message_id = demo_id("inbox-message", 1 + offset)
        draft_id = demo_id("inbox-draft", 1 + offset)
        customer = f"lead{offset + 1:03d}@example.test"
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            await s.execute(
                insert(mailbox_directory)
                .values(address_hash=address_hash(address), tenant_id=tenant.id, mailbox_id=mailbox_id)
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(mailboxes)
                .values(
                    id=mailbox_id,
                    tenant_id=tenant.id,
                    address=address,
                    display_name=f"{tenant.name} support",
                    agent_id=demo_id("agent", 2 + offset),
                    auto_draft=True,
                )
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(threads)
                .values(
                    id=thread_id,
                    tenant_id=tenant.id,
                    mailbox_id=mailbox_id,
                    subject="Question about weekend appointments",
                    contact_id=demo_id("contact", 1 + offset),
                    status="awaiting_review",
                    intent="question",
                    message_count=1,
                    last_message_at=received,
                )
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(messages)
                .values(
                    id=message_id,
                    tenant_id=tenant.id,
                    thread_id=thread_id,
                    mailbox_id=mailbox_id,
                    direction="inbound",
                    rfc_message_id=f"<demo-{offset + 1}@example.test>",
                    from_address=customer,
                    from_name="Demo Customer",
                    to_addresses=[address],
                    subject="Question about weekend appointments",
                    text_body="Hi, do you have any appointments available this Saturday morning? Thanks!",
                    contact_id=demo_id("contact", 1 + offset),
                    received_at=received,
                )
                .on_conflict_do_nothing()
            )
            created = (
                await s.execute(
                    insert(drafts)
                    .values(
                        id=draft_id,
                        tenant_id=tenant.id,
                        thread_id=thread_id,
                        message_id=message_id,
                        mailbox_id=mailbox_id,
                        status="pending_review",
                        current_version=1,
                        intent="question",
                        summary="Customer asks whether Saturday morning appointments are available.",
                    )
                    .on_conflict_do_nothing()
                    .returning(drafts.c.id)
                )
            ).scalar_one_or_none()
            if created is None:
                continue  # already seeded: append-only history is never duplicated
            await s.execute(
                insert(draft_versions).values(
                    tenant_id=tenant.id,
                    draft_id=draft_id,
                    version=1,
                    body_text=(
                        "Hello,\n\nThank you for reaching out. We do offer Saturday morning appointments. "
                        "A member of our team will confirm the available times shortly.\n\nBest regards"
                    ),
                    author_type="ai",
                    author_id="ai-agent-service",
                    model="demo-seed",
                    provider="fake",
                    prompt_version=1,
                    citations=[],
                    quality={"confidence": 0.7, "flags": ["no_knowledge_found"]},
                )
            )
            await s.execute(
                insert(draft_actions).values(
                    tenant_id=tenant.id,
                    draft_id=draft_id,
                    action="generated",
                    version=1,
                    actor_type="system",
                    actor_id="dev-seed",
                )
            )
