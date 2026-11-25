"""ClamAV malware scanning over the clamd TCP protocol (INSTREAM), with a timeout on every step.

Protocol: send `zINSTREAM\\0`, then chunks of <4-byte big-endian length><bytes>, then a zero-length chunk.
clamd replies `stream: OK`, `stream: <signature> FOUND` or `... ERROR` (NUL-terminated).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import struct
from dataclasses import dataclass

CHUNK = 64 * 1024
_FOUND = re.compile(r"^stream: (?P<sig>[A-Za-z0-9._:/-]{1,190}) FOUND$")


class ScannerUnavailable(Exception):
    """clamd could not be reached or returned an error; the caller decides skipped (dev) vs error (prod)."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    infected: bool
    signature: str | None = None


async def scan_bytes(host: str | None, port: int, data: bytes, *, timeout_seconds: float = 30.0) -> ScanResult:
    timeout = timeout_seconds
    if not host:
        raise ScannerUnavailable("scanner_not_configured")
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, TimeoutError) as exc:
        raise ScannerUnavailable("scanner_unreachable") from exc
    try:
        writer.write(b"zINSTREAM\0")
        for offset in range(0, len(data), CHUNK):
            chunk = data[offset : offset + CHUNK]
            writer.write(struct.pack("!L", len(chunk)) + chunk)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        writer.write(struct.pack("!L", 0))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        reply = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=timeout)
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise ScannerUnavailable("scanner_io_error") from exc
    finally:
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
    text = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
    if text == "stream: OK":
        return ScanResult(False)
    found = _FOUND.match(text)
    if found:
        return ScanResult(True, found.group("sig"))
    raise ScannerUnavailable("scanner_error")
