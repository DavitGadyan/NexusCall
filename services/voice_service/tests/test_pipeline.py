"""Conversation engine tests with in-process fakes: barge-in, deterministic opt-out, handoff, timeouts, fallbacks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from nexus_providers.speech import TranscriptEvent
from voice_service.audio import CallRecorder
from voice_service.pipeline import (
    FALLBACK_LINE,
    HANDOFF_LINE,
    NO_SPEECH_GOODBYE,
    OPT_OUT_CONFIRMATION,
    REPROMPT_LINE,
    Conversation,
    ConversationConfig,
)


class FakeStt:
    def __init__(self) -> None:
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.audio = 0
        self.closed = False

    async def send_audio(self, mulaw_8k: bytes) -> None:
        self.audio += len(mulaw_8k)

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            item = await self.q.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self) -> None:
        self.closed = True

    def say(self, text: str) -> None:
        self.q.put_nowait(TranscriptEvent(text=text, is_final=False))
        self.q.put_nowait(TranscriptEvent(text=text, is_final=True, speech_final=True))

    def interim(self, text: str) -> None:
        self.q.put_nowait(TranscriptEvent(text=text, is_final=False))


class FakeSink:
    """Acknowledges marks after `ack_delay` like Twilio does after playback."""

    def __init__(self, ack_delay: float = 0.0) -> None:
        self.media = 0
        self.marks: list[str] = []
        self.clears = 0
        self.ack_delay = ack_delay
        self.conv: Conversation | None = None

    async def send_media(self, audio: bytes) -> None:
        self.media += len(audio)

    async def send_mark(self, name: str) -> None:
        self.marks.append(name)

        async def ack() -> None:
            await asyncio.sleep(self.ack_delay)
            if self.conv is not None:
                self.conv.on_mark(name)

        asyncio.get_running_loop().create_task(ack())

    async def send_clear(self) -> None:
        self.clears += 1


class Hooks:
    def __init__(self, handoff_status: str = "transferring") -> None:
        self.segments: list[tuple[str, str, bool]] = []
        self.opt_outs = 0
        self.handoffs: list[str] = []
        self.handoff_status = handoff_status
        self.recording_stopped = False

    async def on_segment(self, role: str, text: str, *, interrupted: bool, started_ms: int) -> None:
        self.segments.append((role, text, interrupted))

    async def on_opt_out(self) -> None:
        self.opt_outs += 1

    async def on_handoff(self, trigger: str, reason: str) -> str:
        self.handoffs.append(trigger)
        return self.handoff_status

    async def on_handoff_executed(self) -> None:
        self.handoffs.append("agent_tool")

    async def on_stop_recording(self) -> None:
        self.recording_stopped = True

    def said(self) -> list[str]:
        return [t for role, t, _ in self.segments if role == "agent"]


class Model:
    """Scripted gateway stream. Records the history it was called with and whether it was cancelled."""

    def __init__(self, replies: list[list[dict[str, Any]]], delay: float = 0.0) -> None:
        self.replies = replies
        self.calls: list[list[Any]] = []
        self.cancelled = 0
        self.delay = delay

    def __call__(self, history: list[Any]) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(list(history))
        events = self.replies.pop(0) if self.replies else [{"type": "text_delta", "text": "Okay."}, {"type": "done"}]
        return self._gen(events)

    async def _gen(self, events: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        try:
            for ev in events:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield ev
        except (asyncio.CancelledError, GeneratorExit):
            self.cancelled += 1
            raise


class Tts:
    def __init__(self, fail: bool = False, chunk_delay: float = 0.0) -> None:
        self.texts: list[str] = []
        self.fail = fail
        self.chunk_delay = chunk_delay
        self.cancelled = 0

    def __call__(self, text: str) -> AsyncIterator[bytes]:
        self.texts.append(text)
        return self._gen(text)

    async def _gen(self, text: str) -> AsyncIterator[bytes]:
        if self.fail:
            raise RuntimeError("tts down")
        try:
            for _ in range(4):
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                yield b"\x7f" * 800
        except (asyncio.CancelledError, GeneratorExit):
            self.cancelled += 1
            raise


def _conv(
    model: Model, *, stt=None, sink=None, tts=None, hooks=None, **cfg: Any
) -> tuple[Conversation, FakeStt, FakeSink, Tts, Hooks]:
    stt = stt or FakeStt()
    sink = sink or FakeSink()
    tts = tts or Tts()
    hooks = hooks or Hooks()
    config = ConversationConfig(
        intro=cfg.pop("intro", ["This is an AI assistant.", "Hi, this is Acme."]),
        no_speech_timeout=cfg.pop("no_speech_timeout", 5.0),
        turn_timeout=cfg.pop("turn_timeout", 2.0),
        **cfg,
    )
    conv = Conversation(stt=stt, sink=sink, tts=tts, model=model, hooks=hooks, config=config, recorder=CallRecorder(60))
    sink.conv = conv
    return conv, stt, sink, tts, hooks


async def _until(predicate, limit: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_intro_then_turn_then_caller_hangs_up():
    model = Model([[{"type": "text_delta", "text": "Sure. Tuesday works."}, {"type": "done"}]])
    conv, stt, sink, tts, hooks = _conv(model)
    task = asyncio.create_task(conv.run())
    await _until(lambda: len(tts.texts) >= 2)
    stt.say("I want to book an appointment")
    await _until(lambda: len(model.calls) == 1 and "Tuesday works." in tts.texts)
    conv.on_stop()
    assert await asyncio.wait_for(task, 3) == "caller_hangup"
    assert tts.texts[:2] == ["This is an AI assistant.", "Hi, this is Acme."]
    assert tts.texts[2:] == ["Sure.", "Tuesday works."]  # sentence-chunked TTS
    history = model.calls[0]
    assert history[-1].role == "caller" and history[-1].text == "I want to book an appointment"
    assert ("caller", "I want to book an appointment", False) in hooks.segments
    assert len(sink.marks) == 4


async def test_barge_in_clears_twilio_and_cancels_tts_and_model_stream():
    long_reply = [{"type": "text_delta", "text": f"Sentence number {i}. "} for i in range(20)] + [{"type": "done"}]
    model = Model([long_reply], delay=0.01)
    sink = FakeSink(ack_delay=1.0)  # playback is "slow": the agent is still speaking when the caller talks
    tts = Tts(chunk_delay=0.02)
    conv, stt, sink, tts, hooks = _conv(model, sink=sink, tts=tts, intro=[])
    task = asyncio.create_task(conv.run())
    stt.say("Tell me about your services")
    await _until(lambda: len(tts.texts) >= 2)
    stt.interim("Wait, sorry")
    await _until(lambda: sink.clears == 1)
    await _until(lambda: model.cancelled == 1)
    spoken_before = len(tts.texts)
    await asyncio.sleep(0.2)
    assert len(tts.texts) == spoken_before  # nothing more is synthesized for the cancelled turn
    assert conv.barge_ins == 1
    interrupted = [s for s in hooks.segments if s[0] == "agent"]
    assert interrupted and interrupted[-1][2] is True  # partial agent turn recorded as interrupted
    stt.q.put_nowait(TranscriptEvent(text="Wait, sorry, what are your hours?", is_final=True, speech_final=True))
    await _until(lambda: len(model.calls) == 2)
    conv.on_stop()
    await asyncio.wait_for(task, 3)


async def test_intro_is_not_interruptible():
    sink = FakeSink(ack_delay=0.5)
    conv, stt, sink, tts, hooks = _conv(Model([]), sink=sink)
    task = asyncio.create_task(conv.run())
    await _until(lambda: len(tts.texts) >= 1)
    stt.interim("hello hello")
    await asyncio.sleep(0.1)
    assert sink.clears == 0
    conv.on_stop()
    await asyncio.wait_for(task, 3)


async def test_opt_out_is_deterministic_and_never_reaches_the_model():
    model = Model([])
    conv, stt, sink, tts, hooks = _conv(model, intro=["Hi."])
    task = asyncio.create_task(conv.run())
    await _until(lambda: tts.texts == ["Hi."])
    stt.say("Please stop calling me and remove me from your list")
    assert await asyncio.wait_for(task, 3) == "opted_out"
    assert model.calls == []
    assert hooks.opt_outs == 1
    assert tts.texts[-1] == OPT_OUT_CONFIRMATION


async def test_opt_out_during_agent_speech_still_wins():
    model = Model(
        [[{"type": "text_delta", "text": "Let me tell you about our offers. "} for _ in range(10)]], delay=0.02
    )
    sink = FakeSink(ack_delay=0.5)
    conv, stt, sink, tts, hooks = _conv(model, sink=sink, intro=[])
    task = asyncio.create_task(conv.run())
    stt.say("What do you sell?")
    await _until(lambda: len(tts.texts) >= 1)
    stt.say("Stop calling me")
    assert await asyncio.wait_for(task, 4) == "opted_out"
    assert sink.clears >= 1 and hooks.opt_outs == 1 and len(model.calls) == 1


async def test_handoff_request_and_unavailable_fallback():
    conv, stt, sink, tts, hooks = _conv(Model([]), intro=[])
    task = asyncio.create_task(conv.run())
    stt.say("Can I talk to a real person")
    assert await asyncio.wait_for(task, 3) == "handoff"
    assert hooks.handoffs == ["caller_request"] and HANDOFF_LINE in tts.texts

    hooks2 = Hooks(handoff_status="unavailable")
    conv2, stt2, _, tts2, _ = _conv(Model([]), hooks=hooks2, intro=[])
    task2 = asyncio.create_task(conv2.run())
    stt2.say("transfer me to an agent")
    await _until(lambda: len(tts2.texts) == 2)
    assert conv2.end_reason is None  # the conversation continues
    conv2.on_stop()
    await asyncio.wait_for(task2, 3)


async def test_agent_tool_handoff_ends_the_stream():
    reply = [
        {"type": "text_delta", "text": "Connecting you now."},
        {"type": "tool_call", "tool_id": "call.transfer_to_human", "status": "executed"},
        {"type": "done"},
    ]
    conv, stt, sink, tts, hooks = _conv(Model([reply]), intro=[])
    task = asyncio.create_task(conv.run())
    stt.say("I have a complaint")
    assert await asyncio.wait_for(task, 3) == "handoff"
    assert hooks.handoffs == ["agent_tool"]


async def test_stop_recording_discards_audio():
    conv, stt, sink, tts, hooks = _conv(Model([]), intro=[])
    task = asyncio.create_task(conv.run())
    await conv.on_media(b"\x01" * 1600)
    stt.say("please do not record this call")
    await _until(lambda: hooks.recording_stopped)
    await _until(lambda: len(tts.texts) == 1)
    assert conv.recorder is not None and not conv.recorder.enabled and conv.recorder.seconds == 0
    conv.on_stop()
    await asyncio.wait_for(task, 3)


@pytest.mark.parametrize(
    "reply",
    [
        [{"type": "error", "code": "provider_unavailable"}],
        [{"type": "text_delta", "text": "Half"}],  # stream then stalls past the turn deadline
    ],
    ids=["gateway_error", "gateway_stall"],
)
async def test_model_failure_says_fallback_and_ends(reply):
    model = Model([reply], delay=0.0 if reply[0]["type"] == "error" else 0.0)
    if reply[0]["type"] != "error":

        def stalled(history):
            async def gen():
                yield reply[0]
                await asyncio.sleep(10)

            model.calls.append(history)
            return gen()

        conv, stt, sink, tts, hooks = _conv(stalled, intro=[], turn_timeout=0.2)  # type: ignore[arg-type]
    else:
        conv, stt, sink, tts, hooks = _conv(model, intro=[])
    task = asyncio.create_task(conv.run())
    stt.say("Hello?")
    assert await asyncio.wait_for(task, 4) == "provider_failure"
    assert tts.texts[-1] == FALLBACK_LINE


async def test_stt_failure_says_fallback():
    conv, stt, sink, tts, hooks = _conv(Model([]), intro=[])
    task = asyncio.create_task(conv.run())
    stt.q.put_nowait(RuntimeError("deepgram dropped"))
    assert await asyncio.wait_for(task, 3) == "stt_failure"
    assert tts.texts == [FALLBACK_LINE]


async def test_tts_failure_ends_gracefully():
    conv, *_ = _conv(Model([]), tts=Tts(fail=True))
    assert await asyncio.wait_for(conv.run(), 3) == "provider_failure"


async def test_no_speech_reprompts_then_ends():
    conv, stt, sink, tts, hooks = _conv(Model([]), intro=["Hi."], no_speech_timeout=0.2, reprompts=1)
    assert await asyncio.wait_for(conv.run(), 4) == "no_speech"
    assert tts.texts == ["Hi.", REPROMPT_LINE, NO_SPEECH_GOODBYE]


async def test_max_duration_ends_the_call():
    conv, *_ = _conv(Model([]), intro=[], max_seconds=0.2, no_speech_timeout=10)
    assert await asyncio.wait_for(conv.run(), 3) == "max_duration"


async def test_end_after_intro_for_unconfigured_agent():
    conv, stt, sink, tts, hooks = _conv(
        Model([]), intro=["AI disclosure.", FALLBACK_LINE], end_after_intro="config_unavailable"
    )
    assert await asyncio.wait_for(conv.run(), 3) == "config_unavailable"
    assert tts.texts == ["AI disclosure.", FALLBACK_LINE]
