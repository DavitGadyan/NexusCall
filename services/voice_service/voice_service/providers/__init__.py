"""Speech adapters: Deepgram streaming STT and ElevenLabs streaming TTS (`ulaw_8000`).

The fakes are the same clients pointed at the DEVELOPMENT-ONLY simulators in `fake_providers`
(refused outside development/test by `select_adapter`)."""

from __future__ import annotations

import httpx

from nexus_core.providers import select_adapter
from voice_service.providers.deepgram import DeepgramStt
from voice_service.providers.elevenlabs import ElevenLabsTts
from voice_service.settings import Settings


def _ws_base(http_url: str) -> str:
    return "ws" + http_url[4:] if http_url.startswith("http") else http_url


def select_stt(settings: Settings) -> DeepgramStt:
    fake_base = _ws_base(settings.fake_providers_url.rstrip("/")) + "/deepgram"
    return select_adapter(
        settings,
        real=lambda: DeepgramStt(
            settings.deepgram_ws_base, model=settings.deepgram_model, endpointing_ms=settings.endpointing_ms
        ),
        fake=lambda: DeepgramStt(
            fake_base, model=settings.deepgram_model, endpointing_ms=settings.endpointing_ms, name="deepgram-fake"
        ),
        name="stt",
    )


def select_tts(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> ElevenLabsTts:
    return select_adapter(
        settings,
        real=lambda: ElevenLabsTts(
            settings.elevenlabs_base,
            model_id=settings.elevenlabs_model,
            timeout=settings.provider_timeout_seconds,
            transport=transport,
        ),
        fake=lambda: ElevenLabsTts(
            f"{settings.fake_providers_url.rstrip('/')}/elevenlabs",
            model_id=settings.elevenlabs_model,
            timeout=settings.provider_timeout_seconds,
            transport=transport,
            name="elevenlabs-fake",
        ),
        name="tts",
    )
