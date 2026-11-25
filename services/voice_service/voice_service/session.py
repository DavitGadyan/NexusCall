"""One authenticated media stream = one voice session: setup, side effects (hooks) and finalization."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import WebSocket
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.context import Actor
from nexus_core.credentials import ProviderNotConfigured
from nexus_core.storage import tenant_key
from voice_service.audio import CallRecorder
from voice_service.deps import SERVICE_ACTOR, Deps, audit, emit, job_ctx
from voice_service.llm import GatewayStream, TurnRequest, build_body
from voice_service.pipeline import FALLBACK_LINE, Conversation, ConversationConfig, NullStt
from voice_service.providers import select_stt, select_tts
from voice_service.providers.deepgram import SttUnavailable
from voice_service.stream_auth import StreamClaims
from voice_service.tables import handoffs, recordings, session_directory, transcript_segments, voice_sessions

log = logging.getLogger(__name__)

DEFAULT_DISCLOSURE = "Hello. Please note that you are speaking with an automated AI assistant."
CONSENT_PROMPT = (
    "This call may be recorded for quality and training purposes. Say do not record at any time to stop the recording."
)
LIVE_STATES = ("ringing", "connected", "in_progress")
FAILED_REASONS = frozenset({"provider_failure", "stt_failure", "config_unavailable"})
CALLER_ENDED = frozenset({"caller_hangup", "handoff"})


class SessionRejected(Exception):
    pass


class TwilioSink:
    """Outbound Twilio Media Streams messages (`media`, `mark`, `clear`)."""

    def __init__(self, ws: WebSocket, stream_sid: str) -> None:
        self.ws, self.stream_sid = ws, stream_sid

    async def send_media(self, audio: bytes) -> None:
        payload = base64.b64encode(audio).decode()
        await self.ws.send_text(
            json.dumps({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})
        )

    async def send_mark(self, name: str) -> None:
        await self.ws.send_text(json.dumps({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}))

    async def send_clear(self) -> None:
        await self.ws.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))


class VoiceCallSession:
    def __init__(
        self, deps: Deps, claims: StreamClaims, stream_sid: str, *, gateway: GatewayStream, tts: Any = None
    ) -> None:
        self.deps, self.claims, self.stream_sid = deps, claims, stream_sid
        self.gateway = gateway
        self.tts_adapter = tts or select_tts(deps.settings)
        self.tenant_id = claims.tenant_id
        self.call_id = claims.call_id
        self.session_id: uuid.UUID | None = None
        self.agent_id: uuid.UUID | None = claims.agent_id
        self.contact_id: uuid.UUID | None = claims.contact_id
        self.actor = Actor("service", "voice-service")
        self.recorder: CallRecorder | None = None
        self._seq = 0
        self._handoffs = 0
        self.stt_session: Any = None

    # ------------------------------------------------------------------ setup
    async def _call_state(self) -> dict[str, Any]:
        try:
            call = await self.deps.client("call-service").get_json(
                f"/internal/v1/calls/{self.call_id}", tenant_id=self.tenant_id, actor=self.actor
            )
        except (UpstreamError, UpstreamUnavailable) as exc:
            raise SessionRejected("call_unavailable") from exc
        if call.get("state") not in LIVE_STATES or call.get("direction") != self.claims.direction:
            raise SessionRejected("call_not_live")
        return dict(call)

    async def _runtime_config(self) -> dict[str, Any] | None:
        if self.agent_id is None:
            return None
        try:
            config = await self.deps.client("ai-agent-service").get_json(
                f"/internal/v1/agents/{self.agent_id}/runtime-config", tenant_id=self.tenant_id, actor=self.actor
            )
        except (UpstreamError, UpstreamUnavailable):
            return None
        return dict(config) if config.get("kind", "voice") == "voice" else None

    async def _communication(self) -> dict[str, Any]:
        try:
            return await tenant_settings_section(
                self.deps.client("tenant-service"), self.deps.redis, self.tenant_id, "communication"
            )
        except (UpstreamError, UpstreamUnavailable):
            # Conservative defaults: always disclose AI, never record without the tenant's settings.
            return {"ai_disclosure_enabled": True, "recording_enabled": False, "recording_consent_prompt": True}

    async def _key(self, provider: str) -> str:
        resolved = await self.deps.credentials.resolve(
            self.tenant_id, provider, actor=self.actor, allow_platform_default=True
        )
        return resolved.secret("fake_secret" if resolved.source == "fake" else "api_key")

    async def _create_row(self, recording: bool, disclosure: bool, consent: bool, version: int | None) -> None:
        async with self.deps.db.tenant_session(self.tenant_id, self.actor) as s:
            session_id = (
                await s.execute(
                    insert(voice_sessions)
                    .values(
                        tenant_id=self.tenant_id,
                        call_id=self.call_id,
                        agent_id=self.agent_id,
                        contact_id=self.contact_id,
                        direction=self.claims.direction,
                        stream_sid=self.stream_sid[:64],
                        recording_enabled=recording,
                        disclosure_played=disclosure,
                        consent_prompt_played=consent,
                        agent_version=version,
                    )
                    .on_conflict_do_nothing(constraint="uq_voice_sessions_call")
                    .returning(voice_sessions.c.id)
                )
            ).scalar_one_or_none()
            if session_id is None:
                raise SessionRejected("session_exists")  # one media stream per call
            await s.execute(insert(session_directory).values(session_id=session_id, tenant_id=self.tenant_id))
        self.session_id = session_id

    async def prepare(self, sink: Any) -> Conversation:
        call = await self._call_state()
        self.agent_id = self.agent_id or (uuid.UUID(call["agent_id"]) if call.get("agent_id") else None)
        if self.contact_id is None and call.get("contact_id"):
            self.contact_id = uuid.UUID(call["contact_id"])
        comm = await self._communication()
        runtime = await self._runtime_config()
        recording = bool(comm.get("recording_enabled"))
        disclosure = bool(comm.get("ai_disclosure_enabled", True))
        consent = recording and bool(comm.get("recording_consent_prompt", True))
        await self._create_row(recording, disclosure, consent, (runtime or {}).get("version"))
        settings = self.deps.settings
        if recording:
            self.recorder = CallRecorder(settings.max_recording_seconds)
        try:
            await self.deps.client("call-service").post_json(
                f"/internal/v1/calls/{self.call_id}/media-started", tenant_id=self.tenant_id, actor=self.actor
            )
        except (UpstreamError, UpstreamUnavailable):
            log.warning("media_started_notify_failed", extra={"call_id": str(self.call_id)})

        tts_key = await self._key("elevenlabs")
        voice_id = str((runtime or {}).get("voice_id") or settings.default_voice_id)

        def tts(text: str) -> Any:
            return self.tts_adapter.synthesize(tts_key, text, voice_id=voice_id)

        intro: list[str] = []
        if disclosure:
            intro.append(str((runtime or {}).get("disclosure_text") or DEFAULT_DISCLOSURE))
        if consent:
            intro.append(CONSENT_PROMPT)
        if runtime is None:
            # No agent configuration: tell the caller and end (fallback), never improvise without a prompt.
            intro.append(FALLBACK_LINE)
        elif runtime.get("greeting"):
            intro.append(str(runtime["greeting"]))

        end_after_intro = "config_unavailable" if runtime is None else None
        try:
            stt_key = await self._key("deepgram")
            self.stt_session = await select_stt(settings).open(stt_key)
        except (SttUnavailable, ProviderNotConfigured):
            log.warning("stt_unavailable_at_start", extra={"call_id": str(self.call_id)})
            self.stt_session = NullStt()
            if runtime is not None:
                intro.append(FALLBACK_LINE)
            end_after_intro = "stt_failure"
        max_seconds = min(
            int((runtime or {}).get("max_call_seconds") or settings.max_call_seconds), settings.max_call_seconds
        )
        request = TurnRequest(
            agent_id=self.agent_id,
            system_prompt=str((runtime or {}).get("system_prompt") or ""),
            greeting=(runtime or {}).get("greeting"),
            tool_ids=list((runtime or {}).get("tool_ids") or []),
            call_id=self.call_id,
            contact_id=self.contact_id,
            model=(runtime or {}).get("model"),
        )

        def model(history: list[Any]) -> Any:
            request.history = list(history)
            body = build_body(
                request,
                max_tokens=settings.llm_max_tokens,
                classification=settings.data_classification,
                max_turns=settings.max_history_turns,
            )
            return self.gateway.stream(body, tenant_id=self.tenant_id, actor=self.actor)

        config = ConversationConfig(
            intro=intro,
            max_seconds=float(max_seconds),
            no_speech_timeout=settings.no_speech_timeout_seconds,
            reprompts=settings.no_speech_reprompts,
            turn_timeout=settings.llm_turn_timeout_seconds,
            barge_in_min_chars=settings.barge_in_min_chars,
            max_history=settings.max_history_turns,
            end_after_intro=end_after_intro,
        )
        return Conversation(
            stt=self.stt_session, sink=sink, tts=tts, model=model, hooks=self, config=config, recorder=self.recorder
        )

    # ------------------------------------------------------------------ hooks
    async def on_segment(self, role: str, text: str, *, interrupted: bool, started_ms: int) -> None:
        if self.session_id is None or not text:
            return
        self._seq += 1
        async with self.deps.db.tenant_session(self.tenant_id, self.actor) as s:
            await s.execute(
                insert(transcript_segments).values(
                    tenant_id=self.tenant_id,
                    session_id=self.session_id,
                    seq=self._seq,
                    role=role,
                    text=text[:4000],
                    started_ms=max(0, started_ms),
                    interrupted=interrupted,
                )
            )

    async def on_opt_out(self) -> None:
        """Opt-out through call-service (call ends opted_out, `call.opted_out` event) and crm (suppression)."""
        async with self.deps.db.tenant_session(self.tenant_id, self.actor) as s:
            await s.execute(
                update(voice_sessions)
                .where(voice_sessions.c.id == self.session_id)
                .values(opted_out=True, updated_at=func.now())
            )
            await audit(s, self.actor, "voice.opt_out.detected", "call", self.call_id, tenant_id=self.tenant_id)
        try:
            await self.deps.client("call-service").post_json(
                f"/internal/v1/calls/{self.call_id}/opt-out", tenant_id=self.tenant_id, actor=self.actor
            )
        except (UpstreamError, UpstreamUnavailable):
            log.error("opt_out_call_service_failed", extra={"call_id": str(self.call_id)})
        if self.contact_id is not None:
            try:
                await self.deps.client("crm-service").post_json(
                    "/internal/v1/suppressions",
                    {
                        "channel": "phone",
                        "contact_id": str(self.contact_id),
                        "reason": "opt_out",
                        "source": {"type": "call", "id": str(self.call_id)},
                    },
                    tenant_id=self.tenant_id,
                    actor=self.actor,
                    idempotency_key=f"call:{self.call_id}:opt_out",
                )
            except (UpstreamError, UpstreamUnavailable):
                # call-service's `call.opted_out` event makes crm add the suppression as well.
                log.error("opt_out_crm_failed", extra={"call_id": str(self.call_id)})

    async def _record_handoff(self, trigger: str, reason: str, status: str) -> None:
        async with self.deps.db.tenant_session(self.tenant_id, self.actor) as s:
            await s.execute(
                insert(handoffs).values(
                    tenant_id=self.tenant_id,
                    session_id=self.session_id,
                    call_id=self.call_id,
                    trigger=trigger,
                    reason=reason[:64],
                    status=status,
                )
            )
            await s.execute(
                update(voice_sessions)
                .where(voice_sessions.c.id == self.session_id)
                .values(handoff_requested=True, updated_at=func.now())
            )
            await emit(
                s,
                "voice.handoff.requested",
                tenant_id=self.tenant_id,
                actor=self.actor,
                payload={"call_id": self.call_id},
            )

    async def on_handoff(self, trigger: str, reason: str) -> str:
        self._handoffs += 1
        status = "unavailable"
        try:
            result = await self.deps.client("call-service").post_json(
                f"/internal/v1/calls/{self.call_id}/transfer",
                {"reason": reason},
                tenant_id=self.tenant_id,
                actor=self.actor,
                idempotency_key=f"voice:{self.session_id}:handoff:{self._handoffs}",
            )
            status = "transferring" if (result or {}).get("status") == "transferring" else "unavailable"
        except (UpstreamError, UpstreamUnavailable):
            status = "failed"
        await self._record_handoff(trigger, reason, status)
        return "transferring" if status == "transferring" else "unavailable"

    async def on_handoff_executed(self) -> None:
        await self._record_handoff("agent_tool", "agent_tool", "transferring")

    async def on_stop_recording(self) -> None:
        async with self.deps.db.tenant_session(self.tenant_id, self.actor) as s:
            await s.execute(
                update(voice_sessions)
                .where(voice_sessions.c.id == self.session_id)
                .values(recording_enabled=False, updated_at=func.now())
            )
            await audit(s, self.actor, "call.recording.declined", "call", self.call_id, tenant_id=self.tenant_id)

    # ------------------------------------------------------------------ end of call
    async def _segments(self) -> list[dict[str, Any]]:
        async with self.deps.db.tenant_session(self.tenant_id, self.actor) as s:
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
                        .where(transcript_segments.c.session_id == self.session_id)
                        .order_by(transcript_segments.c.seq)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def finalize(self, conversation: Conversation | None, reason: str) -> None:
        if self.stt_session is not None:
            await self.stt_session.close()
        if self.session_id is None:
            return
        reason = reason[:32]
        segments = await self._segments()
        transcript_key = None
        try:
            transcript_key = tenant_key(self.tenant_id, "calls", str(self.call_id), "transcript.json")
            doc = {"call_id": str(self.call_id), "session_id": str(self.session_id), "segments": segments}
            await self.deps.store.put(
                "transcripts", transcript_key, json.dumps(doc).encode(), content_type="application/json"
            )
        except Exception:
            log.exception("transcript_upload_failed", extra={"call_id": str(self.call_id)})
            transcript_key = None
        recording_row: dict[str, Any] | None = None
        if self.recorder is not None and self.recorder.enabled and self.recorder.seconds > 0:
            key = tenant_key(self.tenant_id, "calls", str(self.call_id), "recording.wav")
            data = self.recorder.wav()
            try:
                await self.deps.store.put("recordings", key, data, content_type="audio/wav")
                recording_row = {"object_key": key, "size": len(data), "seconds": self.recorder.seconds}
            except Exception:
                log.exception("recording_upload_failed", extra={"call_id": str(self.call_id)})
        has_caller = any(seg["role"] == "caller" for seg in segments)
        postprocess = "queued" if has_caller and self.agent_id else "skipped"
        now = datetime.now(UTC)
        async with self.deps.db.tenant_session(self.tenant_id, self.actor) as s:
            await s.execute(
                update(voice_sessions)
                .where(voice_sessions.c.id == self.session_id)
                .values(
                    status="failed" if reason in FAILED_REASONS else "ended",
                    end_reason=reason,
                    ended_at=func.now(),
                    turns=conversation.turns if conversation else 0,
                    barge_ins=conversation.barge_ins if conversation else 0,
                    transcript_key=transcript_key,
                    postprocess_status=postprocess,
                    version=voice_sessions.c.version + 1,
                    updated_at=func.now(),
                )
            )
            if recording_row is not None:
                await s.execute(
                    insert(recordings).values(
                        tenant_id=self.tenant_id,
                        session_id=self.session_id,
                        call_id=self.call_id,
                        bucket="recordings",
                        object_key=recording_row["object_key"],
                        content_type="audio/wav",
                        channels=2,
                        duration_seconds=recording_row["seconds"],
                        size_bytes=recording_row["size"],
                    )
                )
            await s.execute(
                update(session_directory)
                .where(session_directory.c.session_id == self.session_id)
                .values(
                    ended_at=now, postprocess_due_at=now + timedelta(minutes=2) if postprocess == "queued" else None
                )
            )
            await emit(
                s,
                "voice.session.ended",
                tenant_id=self.tenant_id,
                actor=self.actor,
                payload={
                    "session_id": self.session_id,
                    "call_id": self.call_id,
                    "reason": reason,
                    "turns": conversation.turns if conversation else 0,
                },
            )
        if postprocess == "queued":
            try:
                await self.deps.enqueue(
                    "voice.postprocess", "voice.postprocess", job_ctx(self.tenant_id), session_id=str(self.session_id)
                )
            except Exception:  # noqa: BLE001 - the post-processing sweeper re-enqueues it
                log.warning("postprocess_enqueue_failed", extra={"session_id": str(self.session_id)})
        if reason not in CALLER_ENDED:
            try:
                await self.deps.client("call-service").post_json(
                    f"/internal/v1/calls/{self.call_id}/hangup",
                    {"reason": f"voice_{reason}"[:64]},
                    tenant_id=self.tenant_id,
                    actor=SERVICE_ACTOR,
                )
            except (UpstreamError, UpstreamUnavailable):
                log.warning("hangup_request_failed", extra={"call_id": str(self.call_id)})
