"""ElevenLabs streaming text-to-speech (https://elevenlabs.io/docs/api-reference/text-to-speech/stream).

`output_format=ulaw_8000` yields mu-law 8 kHz audio that goes to Twilio Media Streams unchanged.
Timeouts on every request, no redirects; the API key travels only in the `xi-api-key` header.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx

_VOICE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TtsUnavailable(Exception):
    pass


class ElevenLabsTts:
    def __init__(
        self,
        base_url: str,
        *,
        model_id: str = "eleven_flash_v2_5",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        name: str = "elevenlabs",
    ) -> None:
        self.name = name
        self._base = base_url.rstrip("/")
        self._model = model_id
        self._timeout = timeout
        self._transport = transport

    async def synthesize(self, api_key: str, text: str, *, voice_id: str) -> AsyncIterator[bytes]:
        if not _VOICE.match(voice_id):
            raise TtsUnavailable("invalid_voice_id")
        url = f"{self._base}/v1/text-to-speech/{voice_id}/stream"
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout, transport=self._transport, follow_redirects=False) as client,
                client.stream(
                    "POST",
                    url,
                    params={"output_format": "ulaw_8000"},
                    headers={"xi-api-key": api_key, "accept": "audio/basic"},
                    json={"text": text, "model_id": self._model},
                ) as resp,
            ):
                if resp.status_code != 200:
                    raise TtsUnavailable(f"http_{resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise TtsUnavailable(type(exc).__name__) from exc
