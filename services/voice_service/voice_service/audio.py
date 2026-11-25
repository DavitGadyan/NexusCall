"""mu-law audio helpers: Twilio frame splitting, sentence chunking and a two-channel call recorder."""

from __future__ import annotations

import re
import struct

FRAME_BYTES = 160  # 20 ms at 8 kHz mu-law
SAMPLE_RATE = 8000
MULAW_SILENCE = 0xFF
_SENTENCE_END = re.compile(r"([.!?]+)(\s+|$)")


def frames(audio: bytes, size: int = FRAME_BYTES) -> list[bytes]:
    return [audio[i : i + size] for i in range(0, len(audio), size)]


class SentenceChunker:
    """Turns a token stream into speakable sentences (TTS starts on the first sentence, not the whole reply)."""

    def __init__(self, max_chars: int = 240) -> None:
        self._buffer = ""
        self._max = max_chars

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        out: list[str] = []
        while True:
            match = _SENTENCE_END.search(self._buffer)
            if match is None:
                break
            end = match.end()
            sentence, self._buffer = self._buffer[:end].strip(), self._buffer[end:]
            if sentence:
                out.append(sentence)
        if len(self._buffer) > self._max:  # very long clause: speak up to the last space
            space = self._buffer.rfind(" ", 0, self._max)
            cut = space if space > 0 else self._max
            out.append(self._buffer[:cut].strip())
            self._buffer = self._buffer[cut:]
        return out

    def flush(self) -> list[str]:
        rest, self._buffer = self._buffer.strip(), ""
        return [rest] if rest else []


class CallRecorder:
    """Dual-channel recording: left = caller (inbound track), right = agent (what we sent).

    The inbound track is continuous (Twilio sends 20 ms frames, silence included), so its byte count is the call
    clock; agent audio is placed at that clock position. Bounded by `max_seconds`."""

    def __init__(self, max_seconds: int) -> None:
        self._limit = max_seconds * SAMPLE_RATE
        self.caller = bytearray()
        self.agent = bytearray()
        self.enabled = True

    def add_caller(self, audio: bytes) -> None:
        if self.enabled and len(self.caller) < self._limit:
            self.caller.extend(audio[: self._limit - len(self.caller)])

    def add_agent(self, audio: bytes) -> None:
        if not self.enabled:
            return
        position = len(self.caller)
        if len(self.agent) < position:
            self.agent.extend(bytes([MULAW_SILENCE]) * (position - len(self.agent)))
        if len(self.agent) < self._limit:
            self.agent.extend(audio[: self._limit - len(self.agent)])

    def discard(self) -> None:
        """Caller withdrew consent: drop everything captured so far and stop recording."""
        self.enabled = False
        self.caller.clear()
        self.agent.clear()

    @property
    def seconds(self) -> int:
        return max(len(self.caller), len(self.agent)) // SAMPLE_RATE

    def wav(self) -> bytes:
        n = max(len(self.caller), len(self.agent))
        left = bytes(self.caller) + bytes([MULAW_SILENCE]) * (n - len(self.caller))
        right = bytes(self.agent) + bytes([MULAW_SILENCE]) * (n - len(self.agent))
        interleaved = bytearray(2 * n)
        interleaved[0::2] = left
        interleaved[1::2] = right
        # WAVE_FORMAT_MULAW (7), 2 channels, 8 kHz, 8 bits per sample.
        fmt = struct.pack("<HHIIHH", 7, 2, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 8)
        data = bytes(interleaved)
        return (
            b"RIFF"
            + struct.pack("<I", 4 + (8 + len(fmt)) + (8 + len(data)))
            + b"WAVE"
            + b"fmt "
            + struct.pack("<I", len(fmt))
            + fmt
            + b"data"
            + struct.pack("<I", len(data))
            + data
        )
