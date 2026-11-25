"""SES inbound via SNS. No user auth: authenticity is the SNS signature (BUILD_CONVENTIONS §3 Webhooks).

Returns 2xx fast; parsing, scanning and drafting happen on `emails.inbound_process`. The tenant is resolved
from the signed envelope recipients through `mailbox_directory`, never from request headers or the MIME.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from email_inbox_service.deps import request_deps
from email_inbox_service.inbound import InvalidNotification, confirm_subscription, parse_envelope, record_notification
from nexus_core.errors import Forbidden, Unprocessable
from nexus_core.webhooks import WebhookVerificationError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/ses", tags=["webhooks"])


@router.post("/inbound")
async def ses_inbound(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        envelope = parse_envelope(raw)
    except InvalidNotification as exc:
        raise Unprocessable("Malformed SNS message") from exc
    try:
        await request.app.state.sns_verifier.verify(envelope, tolerance=request.app.state.settings.sns_max_age_seconds)
    except WebhookVerificationError as exc:
        log.warning("sns_verification_failed", extra={"reason": str(exc)[:80]})
        raise Forbidden("SNS message verification failed") from exc
    except Exception as exc:
        log.warning("sns_verification_error", extra={"error_type": type(exc).__name__})
        raise Forbidden("SNS message verification failed") from exc
    kind = envelope.get("Type")
    try:
        if kind == "SubscriptionConfirmation":
            return {"status": await confirm_subscription(request.app.state.settings, envelope)}
        if kind == "UnsubscribeConfirmation":
            return {"status": "ignored"}
        if kind != "Notification":
            raise InvalidNotification("unsupported SNS type")
        return await record_notification(request_deps(request), envelope)
    except InvalidNotification as exc:
        log.warning("sns_notification_rejected", extra={"reason": str(exc)[:80]})
        raise Unprocessable("Unsupported SES notification") from exc
