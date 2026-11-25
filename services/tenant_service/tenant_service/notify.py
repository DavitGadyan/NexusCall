"""Synchronous, authenticated hand-off of invitation emails to notification-service.

The raw invitation link is a bearer secret, so it is sent over the internal
authenticated API only (never via RabbitMQ, logs, or audit metadata).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import FastAPI

log = logging.getLogger(__name__)


async def send_invitation_email(
    app: FastAPI, *, to: str, tenant_name: str, inviter: str | None, role_names: list[str], link: str, expires_at: str
) -> str | None:
    """Returns None on success, otherwise a short, non-sensitive error code."""
    settings = app.state.settings
    try:
        token = await app.state.service_token()
        async with httpx.AsyncClient(timeout=8.0) as c:
            resp = await c.post(
                f"{settings.notification_service_url}/internal/v1/notifications/email",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "template": "invitation",
                    "to": to,
                    "category": "transactional",
                    "variables": {
                        "tenant_name": tenant_name,
                        "inviter": inviter or "Your administrator",
                        "roles": ", ".join(role_names),
                        "link": link,
                        "expires_at": expires_at,
                    },
                },
            )
    except (httpx.HTTPError, RuntimeError) as exc:
        log.warning("invitation_delivery_failed", extra={"error_type": type(exc).__name__})
        return "notification_unavailable"
    if resp.status_code >= 300:
        return f"notification_http_{resp.status_code}"
    return None


def invitation_link(base_url: str, raw_token: str) -> str:
    return f"{base_url.rstrip('/')}/invite/accept?token={raw_token}"


def safe_detail(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in {"token_hash"}}
