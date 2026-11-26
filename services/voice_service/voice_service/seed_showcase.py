"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): AI voice sessions, transcripts and human
handoffs for the answered Acme showcase calls. No recordings (no audio exists).

Idempotent (fixed ids, ON CONFLICT DO NOTHING; transcript rows are written only with their session). Runs as
`voice_app`, so RLS applies. Sessions are ended and post-processed, and none is put in `session_directory`, so
neither the post-processing sweeper nor the retention job acts on them.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.dialects.postgresql import insert

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devshowcase import TENANT, VOICE_AGENT_ID, Scenario, sid
from voice_service.tables import handoffs, transcript_segments, voice_sessions

SEED_ACTOR = Actor("system", "dev-showcase")


async def seed(db: Database, sc: Scenario) -> None:
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        for c in sc.calls:
            if not c.has_session or c.answered_at is None:
                continue
            session_id = sid("voice_session", c.n)
            started = c.answered_at + timedelta(milliseconds=400)
            ended = c.voice_ended_at or c.ended_at
            created = (
                await s.execute(
                    insert(voice_sessions)
                    .values(
                        id=session_id,
                        tenant_id=TENANT.id,
                        call_id=c.id,
                        agent_id=VOICE_AGENT_ID,
                        contact_id=c.contact.id,
                        direction=c.direction,
                        stream_sid=f"MZ{session_id.hex}",
                        status="ended",
                        end_reason=c.voice_end_reason,
                        turns=c.turns,
                        barge_ins=c.barge_ins,
                        opted_out=c.opted_out,
                        handoff_requested=c.handoff_state is not None,
                        recording_enabled=False,
                        disclosure_played=True,
                        consent_prompt_played=False,
                        agent_version=1,
                        postprocess_status="done",
                        postprocess_attempts=1,
                        started_at=started,
                        ended_at=ended,
                        version=3,
                        created_at=started,
                        updated_at=(ended or started) + timedelta(seconds=30),
                    )
                    .on_conflict_do_nothing()
                    .returning(voice_sessions.c.id)
                )
            ).scalar_one_or_none()
            if created is None:
                continue
            for seg in c.segments:
                await s.execute(
                    insert(transcript_segments).values(
                        id=sid("transcript_segment", c.n * 100 + seg.seq),
                        tenant_id=TENANT.id,
                        session_id=session_id,
                        seq=seg.seq,
                        role=seg.role,
                        text=seg.text,
                        started_ms=seg.started_ms,
                        interrupted=seg.interrupted,
                        created_at=started + timedelta(milliseconds=seg.started_ms),
                    )
                )
            if c.handoff_state and c.handoff_at:
                await s.execute(
                    insert(handoffs).values(
                        id=sid("handoff", c.n),
                        tenant_id=TENANT.id,
                        session_id=session_id,
                        call_id=c.id,
                        trigger="caller_request",
                        reason="caller_asked_for_human",
                        status="transferring",
                        created_at=c.handoff_at,
                        updated_at=c.handoff_at + timedelta(seconds=2),
                    )
                )
