"""Live UI updates. Services publish tiny, permission-tagged events to Redis; notification-service
relays them to browsers over one SSE stream and drops events the viewer lacks permission for.

Payloads carry identifiers and enums only (never names, numbers, message text or transcripts).
"""

from __future__ import annotations

import json
import uuid

from redis.asyncio import Redis

MAX_FIELDS = 8


def ui_channel(tenant_id: uuid.UUID | str) -> str:
    return f"ui:{uuid.UUID(str(tenant_id))}"


async def publish_ui_event(
    redis: Redis | None,
    tenant_id: uuid.UUID | str,
    event_type: str,
    *,
    permission: str,
    data: dict[str, str] | None = None,
) -> None:
    """Best-effort fan-out (UI refresh hint). Never rely on it for business state."""
    if redis is None:
        return
    data = data or {}
    if len(data) > MAX_FIELDS or any(not isinstance(v, str) or len(v) > 64 for v in data.values()):
        raise ValueError("UI events carry at most 8 short string identifiers")
    await redis.publish(ui_channel(tenant_id), json.dumps({"type": event_type, "permission": permission, "data": data}))
