"""Verification of the media stream token minted by call-service (see call_service/stream_token.py).

A stream is accepted only if the token: has a valid HMAC under the shared secret, has not expired, names the
same Twilio CallSid as the stream's `start` message, and has not been used before (single use, via Redis).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis


class StreamAuthError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StreamClaims:
    tenant_id: uuid.UUID
    call_id: uuid.UUID
    call_sid: str
    direction: str
    agent_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    expires_at: int
    jti: str


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_stream_token(
    secret: bytes, token: str | None, *, call_sid: str | None, now: float | None = None
) -> StreamClaims:
    if not token or len(token) > 2048 or token.count(".") != 1:
        raise StreamAuthError("missing_or_malformed_token")
    body, mac = token.split(".", 1)
    expected = base64.urlsafe_b64encode(hmac.new(secret, body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    if not hmac.compare_digest(expected, mac):
        raise StreamAuthError("bad_signature")
    try:
        claims: dict[str, Any] = json.loads(_unb64(body))
        parsed = StreamClaims(
            tenant_id=uuid.UUID(claims["tid"]),
            call_id=uuid.UUID(claims["cid"]),
            call_sid=str(claims["sid"]),
            direction=str(claims["dir"]),
            agent_id=uuid.UUID(claims["aid"]) if claims.get("aid") else None,
            contact_id=uuid.UUID(claims["ctid"]) if claims.get("ctid") else None,
            expires_at=int(claims["exp"]),
            jti=str(claims["jti"]),
        )
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise StreamAuthError("malformed_claims") from exc
    if claims.get("v") != 1 or parsed.direction not in ("inbound", "outbound"):
        raise StreamAuthError("unsupported_token")
    if (now or time.time()) > parsed.expires_at:
        raise StreamAuthError("expired")
    if not call_sid or not hmac.compare_digest(parsed.call_sid, call_sid):
        raise StreamAuthError("call_sid_mismatch")
    return parsed


async def claim_once(redis: Redis | None, claims: StreamClaims, now: float | None = None) -> None:
    """Single use: a replayed token (same jti) is refused for as long as it could still be valid."""
    if redis is None:
        return
    ttl = max(1, claims.expires_at - int(now or time.time()) + 5)
    if not await redis.set(f"voice:stream_jti:{claims.jti}", "1", nx=True, ex=ttl):
        raise StreamAuthError("token_replayed")
