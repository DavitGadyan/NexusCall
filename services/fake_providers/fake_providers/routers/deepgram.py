"""DEVELOPMENT/TEST ONLY Deepgram streaming STT simulator (`wss://api.deepgram.com/v1/listen` protocol subset).

Auth: `Authorization: Token <FAKE_PROVIDER_SECRET>`. Audio arrives as binary mu-law frames. Simulated speech
frames (`FSPK` + UTF-8 text, produced by the Twilio simulator's caller) are "transcribed" back to that text;
anything else is treated as silence. Results follow Deepgram's shape: interim results while speech continues,
then a final result with `speech_final: true` after `endpointing` ms of silence.
"""

from __future__ import annotations

import contextlib
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(prefix="/deepgram", tags=["fake-deepgram"])

FRAME = 160
SPEECH_MARK = b"FSPK"


def _secret() -> str:
    return os.environ.get("FAKE_PROVIDER_SECRET") or "fake-provider-secret-dev-only"


def _result(text: str, *, is_final: bool, speech_final: bool, start: float) -> str:
    return json.dumps(
        {
            "type": "Results",
            "channel_index": [0, 1],
            "duration": 0.02,
            "start": round(start, 3),
            "is_final": is_final,
            "speech_final": speech_final,
            "channel": {"alternatives": [{"transcript": text, "confidence": 0.99, "words": []}]},
        }
    )


def frame_text(frame: bytes) -> str | None:
    if not frame.startswith(SPEECH_MARK):
        return None
    return frame[len(SPEECH_MARK) :].rstrip(b"\xff").decode(errors="ignore")


@router.websocket("/v1/listen")
async def listen(ws: WebSocket) -> None:
    auth = ws.headers.get("authorization", "")
    if auth != f"Token {_secret()}" or ws.query_params.get("encoding") not in ("mulaw", None):
        await ws.close(code=1008)
        return
    await ws.accept()
    endpoint_frames = max(1, int(ws.query_params.get("endpointing", "300")) // 20)
    await ws.send_text(json.dumps({"type": "Metadata", "request_id": "fake", "channels": 1}))
    buffer = b""
    utterance = ""
    silence = 0
    elapsed = 0.0
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("text") is not None:
                if json.loads(message["text"]).get("type") == "CloseStream":
                    if utterance:
                        await ws.send_text(_result(utterance, is_final=True, speech_final=True, start=elapsed))
                    await ws.close()
                    return
                continue  # KeepAlive and other control messages
            buffer += message.get("bytes") or b""
            while len(buffer) >= FRAME:
                frame, buffer = buffer[:FRAME], buffer[FRAME:]
                elapsed += 0.02
                text = frame_text(frame)
                if text is not None:
                    utterance += text
                    silence = 0
                    await ws.send_text(_result(utterance, is_final=False, speech_final=False, start=elapsed))
                elif utterance:
                    silence += 1
                    if silence >= endpoint_frames:
                        await ws.send_text(_result(utterance, is_final=True, speech_final=True, start=elapsed))
                        utterance, silence = "", 0
    except (WebSocketDisconnect, RuntimeError):
        with contextlib.suppress(Exception):
            await ws.close()
