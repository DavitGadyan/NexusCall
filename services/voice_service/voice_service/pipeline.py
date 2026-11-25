"""The live conversation: caller audio -> STT -> turn manager -> AI Gateway (streaming) -> sentence TTS -> Twilio.

Turn manager rules:
* endpointing comes from the STT (`speech_final`); finals are buffered into one caller turn;
* **barge-in**: caller speech while the agent is speaking (and the agent's speech is interruptible) sends
  `clear` to Twilio and cancels the TTS and the in-flight model stream;
* **opt-out / handoff / stop-recording are decided deterministically** (`phrases.detect_intent`) before any
  model call. An opt-out is confirmed, recorded through call-service and crm, and ends the call;
* timeouts: no speech (one reprompt, then goodbye), max call duration, model turn deadline;
* any provider failure: say a fallback line (if TTS still works) and end gracefully.

All I/O is injected (`MediaSink`, `Hooks`, `TtsFn`, `TurnFn`, STT session) so the engine is unit-testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nexus_providers.speech import SttSession
from voice_service.audio import CallRecorder, SentenceChunker
from voice_service.llm import Turn
from voice_service.phrases import detect_intent

log = logging.getLogger(__name__)

OPT_OUT_CONFIRMATION = "Understood. We will not call you again, and your number has been removed. Goodbye."
HANDOFF_LINE = "Of course. Let me connect you with a member of our team."
HANDOFF_UNAVAILABLE = "I'm sorry, no one is available to take your call right now. I can keep helping you myself."
STOP_RECORDING_LINE = "Okay. I have stopped recording this call."
FALLBACK_LINE = "I'm sorry, I'm having technical trouble right now. Someone from our team will follow up. Goodbye."
REPROMPT_LINE = "Are you still there?"
NO_SPEECH_GOODBYE = "I haven't heard anything, so I'll end the call now. Goodbye."
MAX_DURATION_LINE = "We have reached the time limit for this call. Thank you for your time. Goodbye."
_STT_FAILED = object()


class MediaSink(Protocol):
    async def send_media(self, audio: bytes) -> None: ...

    async def send_mark(self, name: str) -> None: ...

    async def send_clear(self) -> None: ...


class Hooks(Protocol):
    async def on_segment(self, role: str, text: str, *, interrupted: bool, started_ms: int) -> None: ...

    async def on_opt_out(self) -> None: ...

    async def on_handoff(self, trigger: str, reason: str) -> str: ...

    async def on_handoff_executed(self) -> None: ...

    async def on_stop_recording(self) -> None: ...


TtsFn = Callable[[str], AsyncIterator[bytes]]
TurnFn = Callable[[list[Turn]], AsyncIterator[dict[str, Any]]]


@dataclass
class ConversationConfig:
    intro: list[str] = field(default_factory=list)  # disclosure, consent prompt, greeting (spoken verbatim)
    max_seconds: float = 1800.0
    no_speech_timeout: float = 12.0
    reprompts: int = 1
    turn_timeout: float = 12.0
    barge_in_min_chars: int = 3
    max_history: int = 16
    # Set when the call cannot be served (no agent config, STT down): speak the intro, then end with this reason.
    end_after_intro: str | None = None


class NullStt:
    """Stand-in when the STT provider is unavailable: hears nothing."""

    async def send_audio(self, mulaw_8k: bytes) -> None:
        return None

    async def events(self) -> AsyncIterator[Any]:
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def close(self) -> None:
        return None


class Conversation:
    def __init__(
        self,
        *,
        stt: SttSession,
        sink: MediaSink,
        tts: TtsFn,
        model: TurnFn,
        hooks: Hooks,
        config: ConversationConfig,
        recorder: CallRecorder | None = None,
    ) -> None:
        self.stt, self.sink, self.tts, self.model, self.hooks, self.config = stt, sink, tts, model, hooks, config
        self.recorder = recorder
        self.history: list[Turn] = []
        self.end_reason: str | None = None
        self.turns = 0
        self.barge_ins = 0
        self._started = time.monotonic()
        self._turn_q: asyncio.Queue[object] = asyncio.Queue()
        self._pending_marks: set[str] = set()
        self._mark_seq = 0
        self._speaking = False
        self._interruptible = False
        self._agent_task: asyncio.Task[None] | None = None
        self._spoken: list[str] = []  # sentences of the current agent turn already sent to Twilio
        self._finals: list[str] = []
        self._last_activity = time.monotonic()
        self._stt_task: asyncio.Task[None] | None = None
        self._done = asyncio.Event()

    # ------------------------------------------------------------------ inbound events (from the media socket)
    async def on_media(self, audio: bytes) -> None:
        if self.recorder is not None:
            self.recorder.add_caller(audio)
        if self.end_reason is None:
            try:
                await self.stt.send_audio(audio)
            except Exception:  # noqa: BLE001 - STT down: the main loop says the fallback line
                self._turn_q.put_nowait(_STT_FAILED)

    def on_mark(self, name: str) -> None:
        self._pending_marks.discard(name)
        if not self._pending_marks and not self._speaking:
            self._last_activity = time.monotonic()

    def on_stop(self) -> None:
        self._end("caller_hangup")
        if self._agent_task is not None:
            self._agent_task.cancel()
        self._turn_q.put_nowait(None)

    @property
    def agent_speaking(self) -> bool:
        return self._speaking or bool(self._pending_marks)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def _end(self, reason: str) -> None:
        if self.end_reason is None:
            self.end_reason = reason
        self._done.set()

    # ------------------------------------------------------------------ STT loop
    async def _stt_loop(self) -> None:
        try:
            async for event in self.stt.events():
                text = event.text.strip()
                if text:
                    self._last_activity = time.monotonic()
                if text and self.agent_speaking and self._interruptible and len(text) >= self.config.barge_in_min_chars:
                    await self._barge_in()
                if event.is_final and text:
                    self._finals.append(text)
                if event.speech_final and self._finals:
                    self._turn_q.put_nowait(" ".join(self._finals))
                    self._finals = []
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - provider failure handled by the main loop
            log.warning("stt_stream_failed")
            self._turn_q.put_nowait(_STT_FAILED)

    async def _barge_in(self) -> None:
        self.barge_ins += 1
        self._interruptible = False
        task = self._agent_task
        if task is not None and not task.done():
            task.cancel()  # stops the TTS stream and the in-flight model stream
        with contextlib.suppress(Exception):
            await self.sink.send_clear()
        self._pending_marks.clear()
        self._speaking = False
        log.info("barge_in", extra={"count": self.barge_ins})

    # ------------------------------------------------------------------ speaking
    async def _speak(self, text: str) -> None:
        """Synthesize one sentence and send it to Twilio, followed by a mark (Twilio echoes it after playback)."""
        self._speaking = True
        try:
            async for chunk in self.tts(text):
                if self.recorder is not None:
                    self.recorder.add_agent(chunk)
                for i in range(0, len(chunk), 3200):
                    await self.sink.send_media(chunk[i : i + 3200])
            self._mark_seq += 1
            name = f"m{self._mark_seq}"
            self._pending_marks.add(name)
            await self.sink.send_mark(name)
        finally:
            self._speaking = False

    async def say(self, text: str, *, interruptible: bool = False) -> None:
        self._interruptible = interruptible
        started = self.elapsed_ms
        await self._speak(text)
        self.history.append(Turn("agent", text))
        await self.hooks.on_segment("agent", text, interrupted=False, started_ms=started)

    async def drain(self, limit: float = 8.0) -> None:
        """Wait until Twilio has played everything we sent (marks echoed), bounded."""
        deadline = time.monotonic() + limit
        # Polling is deliberate: marks arrive from another task and the wait is short and bounded.
        while self._pending_marks and time.monotonic() < deadline and self.end_reason != "caller_hangup":  # noqa: ASYNC110
            await asyncio.sleep(0.02)

    # ------------------------------------------------------------------ agent turn
    async def _agent_turn(self) -> None:
        self._spoken = []
        started = self.elapsed_ms
        chunker = SentenceChunker()
        handoff = False
        completed = False
        events = self.model(self.history[-self.config.max_history :]).__aiter__()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.__anext__(), timeout=self.config.turn_timeout)
                except StopAsyncIteration:
                    break
                kind = event.get("type")
                if kind == "text_delta":
                    for sentence in chunker.feed(str(event.get("text", ""))):
                        self._interruptible = True
                        await self._speak(sentence)
                        self._spoken.append(sentence)
                elif kind == "tool_call":
                    if event.get("tool_id") == "call.transfer_to_human" and event.get("status") == "executed":
                        handoff = True
                elif kind == "error":
                    raise RuntimeError(f"gateway_error:{event.get('code')}")
                elif kind == "done":
                    break
            for sentence in chunker.flush():
                self._interruptible = True
                await self._speak(sentence)
                self._spoken.append(sentence)
            completed = True
        finally:
            with contextlib.suppress(Exception):
                await events.aclose()  # type: ignore[attr-defined]
            text = " ".join(self._spoken)
            if text:
                self.history.append(Turn("agent", text))
                with contextlib.suppress(Exception):
                    await self.hooks.on_segment("agent", text, interrupted=not completed, started_ms=started)
        if handoff:
            await self.hooks.on_handoff_executed()
            await self.drain()
            self._end("handoff")

    async def _respond(self) -> None:
        self._agent_task = asyncio.create_task(self._agent_turn())
        await asyncio.wait({self._agent_task})
        task, self._agent_task = self._agent_task, None
        if task.cancelled():
            return  # barge-in or hangup
        exc = task.exception()
        if exc is not None:
            log.warning("voice_turn_failed", extra={"error_type": type(exc).__name__})
            await self._fail("provider_failure")

    async def _fail(self, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.say(FALLBACK_LINE)
            await self.drain()
        self._end(reason)

    # ------------------------------------------------------------------ turns
    async def _handle_turn(self, text: str) -> None:
        self.turns += 1
        await self.hooks.on_segment("caller", text, interrupted=False, started_ms=self.elapsed_ms)
        self.history.append(Turn("caller", text))
        intent = detect_intent(text)
        if intent == "opt_out":
            # Deterministic: no model involved. Record the opt-out even if the caller hangs up mid-confirmation.
            marking = asyncio.create_task(self.hooks.on_opt_out())
            with contextlib.suppress(Exception):
                await self.say(OPT_OUT_CONFIRMATION)
                await self.drain()
            await asyncio.gather(marking, return_exceptions=True)
            self._end("opted_out")
            return
        if intent == "stop_recording":
            await self.hooks.on_stop_recording()
            if self.recorder is not None:
                self.recorder.discard()
            await self.say(STOP_RECORDING_LINE)
            return
        if intent == "handoff":
            await self.say(HANDOFF_LINE)
            status = await self.hooks.on_handoff("caller_request", "caller_request")
            if status == "transferring":
                await self.drain()
                self._end("handoff")
                return
            await self.say(HANDOFF_UNAVAILABLE, interruptible=True)
            return
        await self._respond()

    async def _next_turn(self) -> object:
        reprompts = self.config.reprompts
        while self.end_reason is None:
            if time.monotonic() - self._started >= self.config.max_seconds:
                return "max_duration"
            if self.agent_speaking:
                self._last_activity = time.monotonic()
            try:
                return await asyncio.wait_for(self._turn_q.get(), timeout=0.1)
            except TimeoutError:
                pass
            if time.monotonic() - self._last_activity >= self.config.no_speech_timeout and not self._finals:
                if reprompts <= 0:
                    return "no_speech"
                reprompts -= 1
                await self.say(REPROMPT_LINE)
                self._last_activity = time.monotonic()
        return None

    async def run(self) -> str:  # noqa: PLR0912 - one linear dispatch over the turn kinds
        self._stt_task = asyncio.create_task(self._stt_loop())
        try:
            for line in self.config.intro:
                if self.end_reason is not None:
                    break
                await self.say(line)
            if self.config.end_after_intro and self.end_reason is None:
                await self.drain()
                self._end(self.config.end_after_intro)
            while self.end_reason is None:
                item = await self._next_turn()
                if item is None:
                    break
                if item is _STT_FAILED:
                    await self._fail("stt_failure")
                elif item == "max_duration":
                    with contextlib.suppress(Exception):
                        await self.say(MAX_DURATION_LINE)
                        await self.drain()
                    self._end("max_duration")
                elif item == "no_speech":
                    with contextlib.suppress(Exception):
                        await self.say(NO_SPEECH_GOODBYE)
                        await self.drain()
                    self._end("no_speech")
                elif isinstance(item, str):
                    await self._handle_turn(item)
        except asyncio.CancelledError:
            self._end("canceled")
            raise
        except Exception:  # noqa: BLE001 - TTS (or another provider) failed outside a model turn
            log.warning("conversation_failed")
            self._end("provider_failure")
        finally:
            if self._stt_task is not None:
                self._stt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._stt_task
        self._end("completed")
        return self.end_reason or "completed"


async def run_with_timeout(conversation: Conversation, extra_seconds: float = 30.0) -> str:
    """Hard stop slightly after the configured maximum, whatever happens inside."""
    try:
        return await asyncio.wait_for(conversation.run(), timeout=conversation.config.max_seconds + extra_seconds)
    except TimeoutError:
        return "max_duration"
