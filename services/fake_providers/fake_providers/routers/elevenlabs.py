"""DEVELOPMENT/TEST ONLY ElevenLabs TTS simulator (`POST /v1/text-to-speech/{voice_id}/stream`, ulaw_8000).

Auth: `xi-api-key: <FAKE_PROVIDER_SECRET>`. The "audio" is deterministic: `FTTS` + the UTF-8 text, then quiet
mu-law filler (0x7F) sized like real speech (~12 characters per second), streamed in chunks. The Twilio
simulator's caller decodes it to record what the agent said.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/elevenlabs", tags=["fake-elevenlabs"])

TTS_MARK = b"FTTS"
BYTES_PER_CHAR = 640  # ~80 ms of 8 kHz mu-law per character
CHUNK = 1600


class TtsIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str = Field(min_length=1, max_length=5000)
    model_id: str | None = None


def _secret() -> str:
    return os.environ.get("FAKE_PROVIDER_SECRET") or "fake-provider-secret-dev-only"


def synth(text: str) -> bytes:
    head = TTS_MARK + text.encode()
    return head + b"\x7f" * max(160, len(text) * BYTES_PER_CHAR - len(head))


@router.post("/v1/text-to-speech/{voice_id}/stream", response_model=None)
async def stream(
    voice_id: str,
    body: TtsIn,
    request: Request,
    xi_api_key: str | None = Header(default=None, alias="xi-api-key"),
) -> StreamingResponse | JSONResponse:
    if xi_api_key != _secret():
        return JSONResponse({"detail": {"status": "invalid_api_key"}}, status_code=401)
    if request.query_params.get("output_format", "ulaw_8000") != "ulaw_8000":
        return JSONResponse({"detail": {"status": "unsupported_output_format"}}, status_code=422)
    if "fail" in voice_id:
        return JSONResponse({"detail": {"status": "internal_error"}}, status_code=500)
    audio = synth(body.text)

    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(audio), CHUNK):
            yield audio[i : i + CHUNK]
            await asyncio.sleep(0)

    return StreamingResponse(chunks(), media_type="audio/basic")
