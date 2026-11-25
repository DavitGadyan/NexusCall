"""Public voice API. Transcript and recording access needs `calls:recordings:read`, is checked at request time
and audited (build.md §5.2). Recordings are served only as short-lived presigned URLs for this tenant's keys."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import NotFound
from nexus_core.storage import assert_tenant_key
from voice_service.deps import AppDeps, audit
from voice_service.tables import handoffs, recordings, transcript_segments, voice_sessions

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])
Recordings = Annotated[AuthContext, Depends(require_permission("calls:recordings:read"))]
Read = Annotated[AuthContext, Depends(require_permission("calls:read"))]


class SegmentOut(BaseModel):
    seq: int
    role: str
    text: str
    started_ms: int
    interrupted: bool


class TranscriptOut(BaseModel):
    call_id: uuid.UUID
    session_id: uuid.UUID
    segments: list[SegmentOut]


class RecordingOut(BaseModel):
    call_id: uuid.UUID
    url: str
    expires_in_seconds: int
    content_type: str
    duration_seconds: int
    channels: int


class HandoffOut(BaseModel):
    trigger: str
    status: str
    created_at: datetime


class SessionOut(BaseModel):
    call_id: uuid.UUID
    session_id: uuid.UUID
    status: str
    end_reason: str | None
    turns: int
    barge_ins: int
    opted_out: bool
    disclosure_played: bool
    consent_prompt_played: bool
    recording_available: bool
    transcript_available: bool
    postprocess_status: str
    started_at: datetime
    ended_at: datetime | None
    handoffs: list[HandoffOut]


async def _session(s: Any, call_id: uuid.UUID) -> dict[str, Any]:
    row = (await s.execute(select(voice_sessions).where(voice_sessions.c.call_id == call_id))).mappings().first()
    if row is None:
        raise NotFound("No voice session for this call")
    return dict(row)


@router.get("/calls/{call_id}", response_model=SessionOut)
async def get_session(call_id: uuid.UUID, request: Request, ctx: Read) -> SessionOut:
    """Session metadata only (no transcript text, no recording)."""
    async with AppDeps(request.app).db.tenant_session(ctx.require_tenant(), ctx.actor) as s:
        sess = await _session(s, call_id)
        rec = (
            await s.execute(
                select(recordings.c.id).where(recordings.c.session_id == sess["id"], recordings.c.status == "stored")
            )
        ).first()
        hand = (
            (
                await s.execute(
                    select(handoffs.c.trigger, handoffs.c.status, handoffs.c.created_at)
                    .where(handoffs.c.session_id == sess["id"])
                    .order_by(handoffs.c.created_at)
                )
            )
            .mappings()
            .all()
        )
    return SessionOut(
        call_id=call_id,
        session_id=sess["id"],
        status=sess["status"],
        end_reason=sess["end_reason"],
        turns=sess["turns"],
        barge_ins=sess["barge_ins"],
        opted_out=sess["opted_out"],
        disclosure_played=sess["disclosure_played"],
        consent_prompt_played=sess["consent_prompt_played"],
        recording_available=rec is not None,
        transcript_available=sess["transcript_purged_at"] is None,
        postprocess_status=sess["postprocess_status"],
        started_at=sess["started_at"],
        ended_at=sess["ended_at"],
        handoffs=[HandoffOut(**dict(h)) for h in hand],
    )


@router.get("/calls/{call_id}/transcript", response_model=TranscriptOut)
async def get_transcript(call_id: uuid.UUID, request: Request, ctx: Recordings) -> TranscriptOut:
    tenant_id = ctx.require_tenant()
    async with AppDeps(request.app).db.tenant_session(tenant_id, ctx.actor) as s:
        sess = await _session(s, call_id)
        if sess["transcript_purged_at"] is not None:
            raise NotFound("Transcript is no longer available (retention)")
        rows = (
            (
                await s.execute(
                    select(
                        transcript_segments.c.seq,
                        transcript_segments.c.role,
                        transcript_segments.c.text,
                        transcript_segments.c.started_ms,
                        transcript_segments.c.interrupted,
                    )
                    .where(transcript_segments.c.session_id == sess["id"])
                    .order_by(transcript_segments.c.seq)
                )
            )
            .mappings()
            .all()
        )
        await audit(s, ctx.actor, "call.transcript.accessed", "call", call_id, tenant_id=tenant_id)
    return TranscriptOut(call_id=call_id, session_id=sess["id"], segments=[SegmentOut(**dict(r)) for r in rows])


@router.get("/calls/{call_id}/recording", response_model=RecordingOut)
async def get_recording(call_id: uuid.UUID, request: Request, ctx: Recordings) -> RecordingOut:
    deps = AppDeps(request.app)
    tenant_id = ctx.require_tenant()
    async with deps.db.tenant_session(tenant_id, ctx.actor) as s:
        rec = (
            (
                await s.execute(
                    select(recordings).where(recordings.c.call_id == call_id, recordings.c.status == "stored")
                )
            )
            .mappings()
            .first()
        )
        if rec is None:
            raise NotFound("No recording for this call")
        key = assert_tenant_key(tenant_id, rec["object_key"])  # defense in depth on top of RLS
        await audit(s, ctx.actor, "call.recording.accessed", "call", call_id, tenant_id=tenant_id)
    seconds = deps.settings.presign_seconds
    url = await deps.store.presign_get(
        rec["bucket"], key, expires_seconds=seconds, download_filename=f"call-{call_id}.wav"
    )
    return RecordingOut(
        call_id=call_id,
        url=url,
        expires_in_seconds=seconds,
        content_type=rec["content_type"],
        duration_seconds=rec["duration_seconds"],
        channels=rec["channels"],
    )
