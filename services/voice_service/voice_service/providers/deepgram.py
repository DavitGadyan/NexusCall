"""Deepgram live transcription over WebSocket (https://developers.deepgram.com/reference/speech-to-text/listen-streaming).

mu-law 8 kHz mono in; `Results` messages out (interim + final, `speech_final` marks endpointing).
The API key is sent only in the `Authorization: Token` header, never in the URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from nexus_providers.speech import TranscriptEvent


class SttUnavailable(Exception):
    pass


class DeepgramSession:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False

    async def send_audio(self, mulaw_8k: bytes) -> None:
        if self._closed:
            return
        try:
            await self._ws.send(mulaw_8k)
        except Exception as exc:
            raise SttUnavailable(type(exc).__name__) from exc

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                data = json.loads(raw)
                if data.get("type") != "Results":
                    continue
                alternatives = (data.get("channel") or {}).get("alternatives") or [{}]
                text = str(alternatives[0].get("transcript") or "")
                yield TranscriptEvent(
                    text=text,
                    is_final=bool(data.get("is_final")),
                    speech_final=bool(data.get("speech_final")),
                    confidence=alternatives[0].get("confidence"),
                )
        except Exception as exc:
            if self._closed:
                return
            raise SttUnavailable(type(exc).__name__) from exc
        if not self._closed:
            raise SttUnavailable("stream_closed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._ws.close(), timeout=2.0)


class DeepgramStt:
    def __init__(
        self, ws_base: str, *, model: str = "nova-3", endpointing_ms: int = 300, name: str = "deepgram"
    ) -> None:
        self.name = name
        self._base = ws_base.rstrip("/")
        self._model = model
        self._endpointing = endpointing_ms

    def url(self, language: str) -> str:
        query = {
            "model": self._model,
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "interim_results": "true",
            "endpointing": str(self._endpointing),
            "punctuate": "true",
            "language": language,
        }
        return f"{self._base}/v1/listen?{urlencode(query)}"

    async def open(self, api_key: str, *, language: str = "en") -> DeepgramSession:
        import websockets

        try:
            ws = await websockets.connect(
                self.url(language),
                additional_headers={"Authorization": f"Token {api_key}"},
                open_timeout=5,
                close_timeout=2,
                max_size=2**20,
            )
        except Exception as exc:
            raise SttUnavailable(type(exc).__name__) from exc
        return DeepgramSession(ws)
