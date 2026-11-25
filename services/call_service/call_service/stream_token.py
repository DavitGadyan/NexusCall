"""Short-lived signed token that authorizes ONE Twilio media stream for ONE call (verified by voice-service).

Format: base64url(json claims) "." base64url(HMAC-SHA256(secret, claims)). Claims carry ids only:
tenant, call, provider call sid, agent, contact, direction, expiry and a unique id (single use in voice-service).
Twilio delivers it to voice-service as a `<Parameter>` of `<Stream>`, so it never appears in a URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def mint_stream_token(
    secret: bytes,
    *,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    call_sid: str,
    direction: str,
    agent_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    ttl_seconds: int = 120,
    now: float | None = None,
) -> str:
    claims: dict[str, Any] = {
        "v": 1,
        "tid": str(tenant_id),
        "cid": str(call_id),
        "sid": call_sid,
        "dir": direction,
        "aid": str(agent_id) if agent_id else None,
        "ctid": str(contact_id) if contact_id else None,
        "exp": int((now or time.time()) + ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"
