"""Telephony adapters. `select_telephony` returns the real Twilio REST adapter, or the same client pointed at the
DEVELOPMENT-ONLY simulator in `fake_providers` (refused outside development/test)."""

from __future__ import annotations

import httpx

from call_service.providers.twilio import TwilioRest
from call_service.settings import Settings
from nexus_core.providers import select_adapter


def select_telephony(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> TwilioRest:
    return select_adapter(
        settings,
        real=lambda: TwilioRest(settings.twilio_api_base, timeout=settings.twilio_timeout_seconds, transport=transport),
        fake=lambda: TwilioRest(
            f"{settings.fake_providers_url.rstrip('/')}/twilio",
            timeout=settings.twilio_timeout_seconds,
            transport=transport,
            token_lookup=True,
            name="twilio-fake",
        ),
        name="telephony",
    )
