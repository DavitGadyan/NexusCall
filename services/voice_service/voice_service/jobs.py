"""Background work: `voice.postprocess` (task) and the post-processing sweeper + retention job (periodic)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update

from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.storage import assert_tenant_key
from voice_service.deps import SERVICE_ACTOR, Deps, job_ctx
from voice_service.tables import recordings, session_directory, transcript_segments, voice_sessions

log = logging.getLogger(__name__)
MAX_POSTPROCESS_SEGMENTS = 200


async def postprocess_session(deps: Deps, ctx: JobContext, session_id: uuid.UUID) -> str:
    """Send the finished transcript to ai-agent `/internal/v1/calls/postprocess` (summary, outcome, qualification;
    ai-agent emits `call.outcome.extracted`). Idempotent: a done session is skipped."""
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        sess = (
            (
                await s.execute(
                    select(voice_sessions)
                    .where(voice_sessions.c.id == session_id, voice_sessions.c.postprocess_status == "queued")
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .first()
        )
        if sess is None:
            return "skipped"
        await s.execute(
            update(voice_sessions)
            .where(voice_sessions.c.id == session_id)
            .values(postprocess_attempts=voice_sessions.c.postprocess_attempts + 1)
        )
        rows = (
            await s.execute(
                select(transcript_segments.c.role, transcript_segments.c.text)
                .where(transcript_segments.c.session_id == session_id)
                .order_by(transcript_segments.c.seq)
                .limit(MAX_POSTPROCESS_SEGMENTS)
            )
        ).all()
    body = {
        "call_id": str(sess["call_id"]),
        "contact_id": str(sess["contact_id"]) if sess["contact_id"] else None,
        "agent_id": str(sess["agent_id"]),
        "transcript": [{"role": r.role, "text": r.text} for r in rows],
    }
    try:
        await deps.client("ai-agent-service").post_json(
            "/internal/v1/calls/postprocess", body, tenant_id=ctx.tenant_id, actor=SERVICE_ACTOR
        )
        status = "done"
    except UpstreamUnavailable as exc:
        raise TransientFailure("ai_agent_unavailable") from exc
    except UpstreamError as exc:
        if exc.upstream_status >= 500 or exc.upstream_status == 429:
            raise TransientFailure("ai_agent_error") from exc
        status = "failed"  # permanent (bad request, agent missing): recorded, not retried
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        await s.execute(
            update(voice_sessions)
            .where(voice_sessions.c.id == session_id)
            .values(postprocess_status=status, updated_at=func.now())
        )
        await s.execute(
            update(session_directory)
            .where(session_directory.c.session_id == session_id)
            .values(postprocess_due_at=None)
        )
    return status


async def postprocess_sweep(deps: Deps, *, now: datetime | None = None) -> int:
    """Re-enqueue post-processing that never ran (crash between commit and publish, or exhausted retries)."""
    now = now or datetime.now(UTC)
    async with deps.db.global_session(SERVICE_ACTOR) as s:
        rows = (
            await s.execute(
                select(session_directory.c.session_id, session_directory.c.tenant_id)
                .where(session_directory.c.postprocess_due_at <= now)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if rows:
            await s.execute(
                update(session_directory)
                .where(session_directory.c.session_id.in_([r.session_id for r in rows]))
                .values(postprocess_due_at=now + timedelta(minutes=10))
            )
    for row in rows:
        await deps.enqueue(
            "voice.postprocess", "voice.postprocess", job_ctx(row.tenant_id), session_id=str(row.session_id)
        )
    return len(rows)


async def retention_purge(deps: Deps, *, now: datetime | None = None) -> int:
    """Delete transcripts and recordings older than each tenant's retention settings (build.md §11)."""
    now = now or datetime.now(UTC)
    async with deps.db.global_session(SERVICE_ACTOR) as s:
        tenants = list(
            (
                await s.execute(
                    select(session_directory.c.tenant_id)
                    .where(session_directory.c.retention_done.is_(False), session_directory.c.ended_at.is_not(None))
                    .distinct()
                )
            ).scalars()
        )
    purged = 0
    for tenant_id in tenants:
        try:
            retention = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "retention")
        except (UpstreamError, UpstreamUnavailable):
            log.warning("retention_settings_unavailable", extra={"tenant_id": str(tenant_id)})
            continue
        purged += await _purge_tenant(deps, tenant_id, retention, now)
    return purged


async def _purge_tenant(deps: Deps, tenant_id: uuid.UUID, retention: dict[str, int], now: datetime) -> int:
    transcripts_cutoff = now - timedelta(days=int(retention.get("transcripts_days") or 180))
    recordings_cutoff = now - timedelta(days=int(retention.get("recordings_days") or 90))
    count = 0
    async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
        old_sessions = (
            (
                await s.execute(
                    select(voice_sessions.c.id, voice_sessions.c.transcript_key).where(
                        voice_sessions.c.ended_at < transcripts_cutoff, voice_sessions.c.transcript_purged_at.is_(None)
                    )
                )
            )
            .mappings()
            .all()
        )
        old_recordings = (
            (
                await s.execute(
                    select(recordings.c.id, recordings.c.bucket, recordings.c.object_key).where(
                        recordings.c.created_at < recordings_cutoff, recordings.c.status == "stored"
                    )
                )
            )
            .mappings()
            .all()
        )
    for sess in old_sessions:
        if sess["transcript_key"]:
            try:
                await deps.store.delete("transcripts", assert_tenant_key(tenant_id, sess["transcript_key"]))
            except Exception:  # noqa: BLE001 - retried on the next run
                log.warning("transcript_delete_failed", extra={"session_id": str(sess["id"])})
                continue
        async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
            await s.execute(delete(transcript_segments).where(transcript_segments.c.session_id == sess["id"]))
            await s.execute(
                update(voice_sessions)
                .where(voice_sessions.c.id == sess["id"])
                .values(transcript_purged_at=func.now(), updated_at=func.now())
            )
        count += 1
    for rec in old_recordings:
        try:
            await deps.store.delete(rec["bucket"], assert_tenant_key(tenant_id, rec["object_key"]))
        except Exception:  # noqa: BLE001 - retried on the next run
            log.warning("recording_delete_failed", extra={"recording_id": str(rec["id"])})
            continue
        async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
            await s.execute(
                update(recordings)
                .where(recordings.c.id == rec["id"])
                .values(status="deleted", deleted_at=func.now(), updated_at=func.now())
            )
        count += 1
    # Sessions whose transcript is purged and which have no stored recording need no further visits.
    async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
        done = list(
            (
                await s.execute(
                    select(voice_sessions.c.id).where(
                        voice_sessions.c.transcript_purged_at.is_not(None),
                        ~select(recordings.c.id)
                        .where(recordings.c.session_id == voice_sessions.c.id, recordings.c.status == "stored")
                        .exists(),
                    )
                )
            ).scalars()
        )
        if done:
            await s.execute(
                update(session_directory).where(session_directory.c.session_id.in_(done)).values(retention_done=True)
            )
    return count
