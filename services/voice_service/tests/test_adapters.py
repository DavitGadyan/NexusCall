"""Speech and gateway adapter contract tests. The real Deepgram client is exercised against the Deepgram simulator
over a loopback WebSocket; ElevenLabs and the AI Gateway client run against mocked HTTP (respx) and the simulator."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import respx

from nexus_core.context import Actor
from voice_service.llm import GatewayError, GatewayStream
from voice_service.providers.deepgram import DeepgramStt, SttUnavailable
from voice_service.providers.elevenlabs import ElevenLabsTts, TtsUnavailable

FAKE_SECRET = "voice-test-fake-secret"  # same value as conftest.FAKE_SECRET

SPEECH = b"FSPK"


def _speech(text: str) -> list[bytes]:
    data = SPEECH + text.encode()
    return [data + b"\xff" * (160 - len(data))]


async def _collect(session, frames: list[bytes]) -> list:
    events = []

    async def reader():
        async for ev in session.events():
            events.append(ev)
            if ev.speech_final:
                return

    task = asyncio.create_task(reader())
    for frame in frames:
        await session.send_audio(frame)
    await asyncio.wait_for(task, 5)
    await session.close()
    return events


def test_deepgram_client_against_simulator(fake_providers_app, server):
    with server(fake_providers_app) as base:
        stt = DeepgramStt("ws" + base[4:] + "/deepgram", endpointing_ms=100)

        async def go():
            session = await stt.open(FAKE_SECRET)
            return await _collect(session, _speech("book me in") + [b"\xff" * 160] * 10)

        events = asyncio.run(go())
        assert events[0].is_final is False and events[0].text == "book me in"
        assert events[-1].is_final and events[-1].speech_final and events[-1].text == "book me in"

        with pytest.raises(SttUnavailable):
            # The simulator closes with 1008 on a bad key: either the handshake or the first read fails.
            async def check():
                session = await stt.open("wrong-key")
                async for _ in session.events():
                    pass

            asyncio.run(check())


def test_deepgram_url_carries_no_secret():
    url = DeepgramStt("wss://api.deepgram.com").url("en")
    assert url.startswith("wss://api.deepgram.com/v1/listen?") and "encoding=mulaw" in url and "sample_rate=8000" in url
    assert "token" not in url.lower() and "key" not in url.lower()


async def test_deepgram_unreachable_is_a_provider_failure():
    with pytest.raises(SttUnavailable):
        await DeepgramStt("ws://127.0.0.1:1").open("k")


@respx.mock
async def test_elevenlabs_real_request_shape_and_errors():
    route = respx.post("https://api.elevenlabs.test/v1/text-to-speech/voice1/stream").mock(
        return_value=httpx.Response(200, content=b"\x7f" * 3200)
    )
    tts = ElevenLabsTts("https://api.elevenlabs.test")
    audio = b"".join([c async for c in tts.synthesize("key-1", "Hello.", voice_id="voice1")])
    assert audio == b"\x7f" * 3200
    sent = route.calls.last.request
    assert sent.url.params["output_format"] == "ulaw_8000" and sent.headers["xi-api-key"] == "key-1"
    assert json.loads(sent.content) == {"text": "Hello.", "model_id": "eleven_flash_v2_5"}
    route.mock(return_value=httpx.Response(401, json={"detail": "bad"}))
    with pytest.raises(TtsUnavailable, match="http_401"):
        [c async for c in tts.synthesize("bad", "Hi.", voice_id="voice1")]
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TtsUnavailable):
        [c async for c in tts.synthesize("k", "Hi.", voice_id="voice1")]
    with pytest.raises(TtsUnavailable, match="invalid_voice_id"):
        [c async for c in tts.synthesize("k", "Hi.", voice_id="../admin")]


async def test_elevenlabs_client_against_simulator(fake_providers_app):
    tts = ElevenLabsTts("http://fake/elevenlabs", transport=httpx.ASGITransport(app=fake_providers_app))
    audio = b"".join([c async for c in tts.synthesize(FAKE_SECRET, "Good morning.", voice_id="v1")])
    assert audio.startswith(b"FTTSGood morning.") and len(audio) > 1000
    with pytest.raises(TtsUnavailable):
        [c async for c in tts.synthesize("wrong", "x", voice_id="v1")]


@respx.mock
async def test_gateway_stream_parses_sse_and_maps_denials():
    url = "http://ai-gateway:8000/internal/v1/ai/responses/stream"
    lines = [
        {"type": "text_delta", "text": "Hello. "},
        {"type": "tool_call", "tool_id": "booking.find_slots", "status": "executed"},
        {"type": "done", "request_id": str(uuid.uuid4()), "stop_reason": "end_turn", "usage": {}},
    ]
    body = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + ": keepalive\n\ndata: not-json\n\n"
    route = respx.post(url).mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )

    async def token() -> str:
        return "svc-token"

    gw = GatewayStream("http://ai-gateway:8000", token)
    tenant = uuid.uuid4()
    events = [
        e async for e in gw.stream({"purpose": "voice_turn"}, tenant_id=tenant, actor=Actor("service", "voice-service"))
    ]
    assert [e["type"] for e in events] == ["text_delta", "tool_call", "done"]
    headers = route.calls.last.request.headers
    assert headers["X-Tenant-ID"] == str(tenant) and headers["Authorization"] == "Bearer svc-token"
    assert headers["X-Actor"] == "service:voice-service"
    route.mock(return_value=httpx.Response(403, json={"code": "policy_denied", "reason": "kill_switch"}))
    with pytest.raises(GatewayError, match="policy_denied"):
        [e async for e in gw.stream({}, tenant_id=tenant, actor=Actor("service", "voice-service"))]
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(GatewayError, match="gateway_unavailable"):
        [e async for e in gw.stream({}, tenant_id=tenant, actor=Actor("service", "voice-service"))]
