"""`POST /webhooks/ses/events`: SES configuration-set events delivered by Amazon SNS. No user auth.

Verification (`SnsVerifier`): signing certificate from an anchored host pattern over HTTPS, SignatureVersion
1/2 signature, timestamp window and the configured topic ARNs. Fake mode accepts only the dev simulator
(`cert_host_pattern=^fake-providers$`, http). SubscriptionConfirmation is confirmed only after verification,
and only by fetching a SubscribeURL on the same allow-listed SNS host. The work done here is small (one
deduplicated transaction); crm suppression pushes run in a periodic job.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from email_service.deps import AppDeps
from email_service.ingest import ingest_ses_event
from email_service.settings import FAKE_SES_EVENTS_TOPIC, Settings
from nexus_core.config import ProviderMode
from nexus_core.webhooks import SNS_CERT_HOST_PATTERN, SnsVerifier, WebhookVerificationError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/ses", tags=["webhooks"], include_in_schema=False)
MAX_BODY = 256 * 1024
FAKE_HOST_PATTERN = r"^fake-providers$"


def build_verifier(settings: Settings) -> SnsVerifier:
    topics = frozenset(settings.ses_events_topic_arns)
    if settings.provider_mode is ProviderMode.FAKE and not settings.is_production_like:
        return SnsVerifier(FAKE_HOST_PATTERN, topics or frozenset({FAKE_SES_EVENTS_TOPIC}), allow_http=True)
    # Real mode: an empty allowlist would accept any topic, so it is refused (fail closed) at request time.
    return SnsVerifier(SNS_CERT_HOST_PATTERN, topics)


def _ok(status: str) -> JSONResponse:
    return JSONResponse({"status": status}, status_code=200)


def _subscribe_url_allowed(url: str, settings: Settings) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if settings.provider_mode is ProviderMode.FAKE and not settings.is_production_like:
        return parts.scheme in ("http", "https") and re.fullmatch(FAKE_HOST_PATTERN, host) is not None
    return parts.scheme == "https" and not parts.username and re.fullmatch(SNS_CERT_HOST_PATTERN, host) is not None


@router.post("/events")
async def ses_events(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"code": "payload_too_large"}, status_code=413)
    try:
        loaded: Any = json.loads(raw)
    except ValueError:
        return JSONResponse({"code": "malformed"}, status_code=400)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("MessageId"), str):
        return JSONResponse({"code": "malformed"}, status_code=400)
    # Every signed SNS field is a string; anything else (e.g. MessageAttributes) is not used.
    message: dict[str, str] = {k: v for k, v in loaded.items() if isinstance(v, str)}
    real_mode = not (settings.provider_mode is ProviderMode.FAKE and not settings.is_production_like)
    if real_mode and not settings.ses_events_topic_arns:
        log.error("ses_events_topic_allowlist_missing")
        return JSONResponse({"code": "not_configured"}, status_code=403)
    try:
        await request.app.state.sns_verifier.verify(message)
    except (WebhookVerificationError, httpx.HTTPError, ValueError) as exc:
        log.warning("ses_event_rejected", extra={"reason": type(exc).__name__})
        return JSONResponse({"code": "invalid_signature"}, status_code=403)

    kind = message.get("Type")
    if kind == "SubscriptionConfirmation":
        url = message.get("SubscribeURL", "")
        if not settings.sns_confirm_subscriptions or not _subscribe_url_allowed(url, settings):
            log.warning("sns_subscription_not_confirmed")
            return _ok("ignored")
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                resp = await client.get(url)
            confirmed = resp.status_code == 200
        except httpx.HTTPError:
            confirmed = False
        log.info("sns_subscription_confirmation", extra={"confirmed": confirmed})
        return _ok("confirmed" if confirmed else "confirmation_failed")
    if kind == "UnsubscribeConfirmation":
        log.warning("sns_unsubscribe_confirmation_received")
        return _ok("ignored")
    if kind != "Notification":
        return _ok("ignored")
    try:
        event = json.loads(message.get("Message", ""))
    except ValueError:
        return _ok("ignored")
    if not isinstance(event, dict):
        return _ok("ignored")
    result = await ingest_ses_event(AppDeps(request.app), sns_message_id=message["MessageId"], event=event)
    return _ok(result.outcome)
