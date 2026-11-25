from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel


class TranscriptEvent(BaseModel):
    text: str
    is_final: bool
    speech_final: bool = False
    confidence: float | None = None


class SttSession(Protocol):
    async def send_audio(self, mulaw_8k: bytes) -> None: ...

    def events(self) -> AsyncIterator[TranscriptEvent]: ...

    async def close(self) -> None: ...


class SttProvider(Protocol):
    name: str

    async def open(self, api_key: str, *, language: str = "en") -> SttSession: ...


class TtsProvider(Protocol):
    name: str

    def synthesize(self, api_key: str, text: str, *, voice_id: str) -> AsyncIterator[bytes]:
        """Yields mu-law 8 kHz audio chunks suitable for Twilio Media Streams."""
        ...
