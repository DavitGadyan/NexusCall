"""Inbound HubSpot webhooks (`POST /webhooks/hubspot`): signature v3, timestamp window, payload validation,
dedupe on the provider event id, tenant resolved from trusted configuration (build.md §3.1M, conventions §3).

Signature v3 (https://developers.hubspot.com/docs/api/webhooks/validating-requests):
base64(HMAC-SHA256(app client secret, method + URI + body + X-HubSpot-Request-Timestamp)), timestamp in ms,
rejected when older than 5 minutes. The URI is our configured public URL, never the Host header.

The tenant comes from `inbound_webhook_registry` (portal id → tenant, written when the OAuth connection was
verified), never from the payload. Work is only recorded here; it returns 204 fast.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from integration_service.deps import db, settings_of
from integration_service.oauth import client_credentials
from integration_service.tables import inbound_webhook_events, inbound_webhook_registry
from nexus_core.context import Actor
from nexus_core.errors import ProblemError, Unauthorized

router = APIRouter(prefix="/webhooks", tags=["inbound-webhooks"], include_in_schema=False)
log = logging.getLogger(__name__)
ACTOR = Actor("service", "hubspot-webhooks")
MAX_EVENTS = 100


class BadPayload(ProblemError):
    status, code, title = 400, "invalid_payload", "Invalid webhook payload"


class HubSpotEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    eventId: int  # noqa: N815 - provider field names
    subscriptionType: str = Field(max_length=64)  # noqa: N815
    portalId: int  # noqa: N815
    objectId: int | None = None  # noqa: N815
    occurredAt: int | None = None  # noqa: N815


def hubspot_signature(secret: str, method: str, uri: str, body: bytes, timestamp: str) -> str:
    message = method.encode() + uri.encode() + body + timestamp.encode()
    return base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()


@router.post("/hubspot", status_code=204)
async def hubspot(request: Request) -> Response:
    settings = settings_of(request)
    body = await request.body()
    timestamp = request.headers.get("x-hubspot-request-timestamp", "")
    signature = request.headers.get("x-hubspot-signature-v3", "")
    if not timestamp.isdigit() or not signature:
        raise Unauthorized("Missing HubSpot signature headers")
    if abs(time.time() * 1000 - int(timestamp)) > settings.hubspot_webhook_tolerance_seconds * 1000:
        raise Unauthorized("Stale HubSpot webhook (possible replay)")
    uri = f"{settings.public_base_url.rstrip('/')}{request.url.path}"
    if request.url.query:
        uri += f"?{request.url.query}"
    secret = (await client_credentials(request.app, "hubspot"))["client_secret"].get_secret_value()
    expected = hubspot_signature(secret, "POST", uri, body, timestamp)
    del secret
    if not hmac.compare_digest(expected, signature):
        raise Unauthorized("HubSpot signature mismatch")
    try:
        raw = json.loads(body)
        if not isinstance(raw, list) or len(raw) > MAX_EVENTS:
            raise BadPayload("Expected a JSON array of at most 100 events")
        events = [HubSpotEvent.model_validate(e) for e in raw]
    except (ValueError, ValidationError) as exc:
        raise BadPayload("Malformed HubSpot event batch") from exc
    await record_events(request, events)
    return Response(status_code=204)


async def record_events(request: Request, events: list[HubSpotEvent]) -> dict[str, int]:
    counts = {"recorded": 0, "duplicate": 0, "unknown_account": 0}
    database = db(request)
    portals = {str(e.portalId) for e in events}
    async with database.global_session(ACTOR) as s:
        mapping: dict[str, Any] = {
            r.external_account_id: r
            for r in (
                await s.execute(
                    select(inbound_webhook_registry).where(
                        inbound_webhook_registry.c.provider == "hubspot",
                        inbound_webhook_registry.c.external_account_id.in_(portals),
                        inbound_webhook_registry.c.enabled.is_(True),
                    )
                )
            ).all()
        }
    for event in events:
        target = mapping.get(str(event.portalId))
        if target is None:  # unknown or disconnected portal: acknowledged and ignored
            counts["unknown_account"] += 1
            continue
        async with database.tenant_session(target.tenant_id, ACTOR) as s:
            inserted = (
                await s.execute(
                    insert(inbound_webhook_events)
                    .values(
                        tenant_id=target.tenant_id,
                        provider="hubspot",
                        connection_id=target.connection_id,
                        provider_event_id=str(event.eventId),
                        event_type=event.subscriptionType,
                        object_id=str(event.objectId) if event.objectId is not None else None,
                        occurred_at=datetime.fromtimestamp(event.occurredAt / 1000, UTC) if event.occurredAt else None,
                    )
                    .on_conflict_do_nothing(constraint="uq_inbound_webhook_events_dedupe")
                    .returning(inbound_webhook_events.c.id)
                )
            ).scalar_one_or_none()
        counts["recorded" if inserted else "duplicate"] += 1
    log.info("hubspot_webhook_batch", extra=counts)
    return counts
