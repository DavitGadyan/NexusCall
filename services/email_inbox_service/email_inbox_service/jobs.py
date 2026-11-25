"""Periodic jobs (ADR-0014): the sweeper re-enqueues stuck work; the retention job purges email content.

Both only select rows and enqueue IDs or clear content; tasks stay idempotent (atomic claims).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import distinct, func, select, update

from email_inbox_service.deps import Deps, audit, job_context
from email_inbox_service.settings import DRAFT_QUEUE, DRAFT_TASK, INBOUND_QUEUE, INBOUND_TASK, SEND_QUEUE, SEND_TASK
from email_inbox_service.tables import attachments, drafts, mailbox_directory, messages, provider_events, send_commands
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.context import Actor

log = logging.getLogger(__name__)
JOB_ACTOR = Actor("system", "email-inbox-jobs")
BATCH = 200


async def _tenants(deps: Deps) -> list[uuid.UUID]:
    async with deps.db.global_session(JOB_ACTOR) as s:
        return list((await s.execute(select(distinct(mailbox_directory.c.tenant_id)))).scalars().all())


async def sweep(deps: Deps) -> dict[str, int]:
    now = datetime.now(UTC)
    due = now - timedelta(seconds=deps.settings.requeue_after_seconds)
    stale = now - timedelta(seconds=deps.settings.stale_seconds)
    counts = {"inbound": 0, "drafts": 0, "sends": 0}
    async with deps.db.global_session(JOB_ACTOR) as s:
        await s.execute(
            update(provider_events)
            .where(provider_events.c.status == "processing", provider_events.c.updated_at < stale)
            .values(status="queued", last_error="stale", updated_at=stale)
        )
        events = (
            await s.execute(
                update(provider_events)
                .where(provider_events.c.status == "queued", provider_events.c.updated_at < due)
                .values(updated_at=func.now())
                .returning(provider_events.c.id, provider_events.c.tenant_id)
            )
        ).all()
    for ev in events[:BATCH]:
        await deps.enqueue(INBOUND_TASK, INBOUND_QUEUE, job_context(ev.tenant_id, JOB_ACTOR), event_id=str(ev.id))
        counts["inbound"] += 1
    for tenant_id in await _tenants(deps):
        async with deps.db.tenant_session(tenant_id, JOB_ACTOR) as s:
            await s.execute(
                update(drafts)
                .where(drafts.c.status == "generating", drafts.c.updated_at < stale)
                .values(status="queued", last_error="stale", updated_at=stale)
            )
            draft_ids = (
                (
                    await s.execute(
                        update(drafts)
                        .where(drafts.c.status == "queued", drafts.c.updated_at < due)
                        .values(updated_at=func.now())
                        .returning(drafts.c.id)
                    )
                )
                .scalars()
                .all()
            )
            # `sending` rows left by a crashed worker go back to the queue; the retry reuses the idempotency key.
            await s.execute(
                update(send_commands)
                .where(send_commands.c.status == "sending", send_commands.c.updated_at < stale)
                .values(status="queued", last_error="stale", updated_at=stale)
            )
            await s.execute(
                update(drafts)
                .where(drafts.c.status == "sending", drafts.c.updated_at < stale)
                .values(status="approved", updated_at=func.now())
            )
            command_ids = (
                (
                    await s.execute(
                        update(send_commands)
                        .where(send_commands.c.status == "queued", send_commands.c.updated_at < due)
                        .values(updated_at=func.now())
                        .returning(send_commands.c.id)
                    )
                )
                .scalars()
                .all()
            )
        for draft_id in draft_ids[:BATCH]:
            await deps.enqueue(DRAFT_TASK, DRAFT_QUEUE, job_context(tenant_id, JOB_ACTOR), draft_id=str(draft_id))
            counts["drafts"] += 1
        for command_id in command_ids[:BATCH]:
            await deps.enqueue(SEND_TASK, SEND_QUEUE, job_context(tenant_id, JOB_ACTOR), command_id=str(command_id))
            counts["sends"] += 1
    return counts


async def retention(deps: Deps) -> dict[str, int]:
    """Purge message bodies, raw MIME and attachment files past the tenant's retention periods. Metadata rows
    stay for threading and audit. Draft versions are immutable history and are NOT purged here."""
    totals = {"messages": 0, "attachments": 0, "provider_events": 0}
    now = datetime.now(UTC)
    for tenant_id in await _tenants(deps):
        try:
            policy = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "retention")
        except (UpstreamError, UpstreamUnavailable):
            log.warning("retention_policy_unavailable", extra={"tenant": str(tenant_id)})
            continue
        content_cutoff = now - timedelta(days=int(policy.get("email_content_days", 365)))
        files_cutoff = now - timedelta(days=int(policy.get("attachments_days", 180)))
        async with deps.db.tenant_session(tenant_id, JOB_ACTOR) as s:
            rows = (
                await s.execute(
                    select(messages.c.id, messages.c.raw_object_key)
                    .where(messages.c.received_at < content_cutoff, messages.c.content_purged_at.is_(None))
                    .limit(1000)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if rows:
                await s.execute(
                    update(messages)
                    .where(messages.c.id.in_([r.id for r in rows]))
                    .values(text_body="", html_sanitized=None, raw_object_key=None, content_purged_at=func.now())
                )
            files = (
                await s.execute(
                    select(attachments.c.id, attachments.c.object_key)
                    .where(attachments.c.created_at < files_cutoff, attachments.c.object_key.is_not(None))
                    .limit(1000)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if files:
                await s.execute(
                    update(attachments)
                    .where(attachments.c.id.in_([f.id for f in files]))
                    .values(object_key=None, scan_status="purged", updated_at=func.now())
                )
            if rows or files:
                await audit(
                    s,
                    JOB_ACTOR,
                    "email.retention.purged",
                    "tenant",
                    tenant_id,
                    tenant_id=tenant_id,
                    messages=len(rows),
                    attachments=len(files),
                )
        if deps.store is not None:
            for key in [r.raw_object_key for r in rows if r.raw_object_key]:
                await _delete(deps, "raw-mail", key)
            for f in files:
                await _delete(deps, "attachments", f.object_key)
        totals["messages"] += len(rows)
        totals["attachments"] += len(files)
    cutoff = now - timedelta(days=deps.settings.provider_event_retention_days)
    async with deps.db.global_session(JOB_ACTOR) as s:
        deleted = (
            await s.execute(
                provider_events.delete()
                .where(
                    provider_events.c.updated_at < cutoff,
                    provider_events.c.status.in_(("processed", "duplicate", "failed")),
                )
                .returning(provider_events.c.id)
            )
        ).all()
    totals["provider_events"] = len(deleted)
    return totals


async def _delete(deps: Deps, bucket: str, key: str) -> None:
    assert deps.store is not None
    try:
        await deps.store.delete(bucket, key)
    except Exception:  # noqa: BLE001 - retried on the next run
        log.warning("retention_delete_failed", extra={"bucket": bucket})
