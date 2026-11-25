"""Public unsubscribe: `GET /u/{token}` shows a confirmation page (GET never changes state: mail scanners
prefetch links), `POST /u/{token}` unsubscribes. The POST is also the RFC 8058 one-click target
(`List-Unsubscribe-Post: List-Unsubscribe=One-Click`), so it needs no cookie, session or CSRF token.

Tokens carry 256 bits of entropy and only their SHA-256 is stored. Unknown and malformed tokens get the same
response. The opt-out applies locally at once (send-time check), is published as `email.unsubscribed`, and
is pushed to crm `suppressions` (inline best effort, then retried by the suppression sync job).
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, update

from email_service.deps import audit, emit
from email_service.jobs import push_suppression
from email_service.messages import transition
from email_service.security import hash_token
from email_service.tables import email_messages, unsubscribe_token_directory, unsubscribe_tokens
from nexus_core.clients import service_client
from nexus_core.context import Actor
from nexus_core.ratelimit import allow_fixed_window

log = logging.getLogger(__name__)
router = APIRouter(prefix="/u", tags=["unsubscribe"], include_in_schema=False)
UNSUBSCRIBER = Actor("system", "unsubscribe-link")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head><body><main><h1>{html.escape(title)}</h1>{body}</main>"
        "</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def _invalid() -> HTMLResponse:
    return _page("Link not valid", "<p>This unsubscribe link is not valid.</p>", 404)


async def _limited(request: Request) -> bool:
    redis = getattr(request.app.state, "redis", None)
    ip = request.client.host if request.client else "unknown"
    try:
        return not await allow_fixed_window(redis, f"rl:unsubscribe:{ip}", 60, 60)
    except Exception:  # noqa: BLE001 - a Redis outage must never block an opt-out
        return False


async def _resolve(request: Request, token: str) -> tuple[uuid.UUID, dict[str, Any]] | None:
    if not _TOKEN.match(token):
        return None
    token_hash = hash_token(token)
    database = request.app.state.db
    async with database.global_session(UNSUBSCRIBER) as s:
        tenant_id = (
            await s.execute(
                select(unsubscribe_token_directory.c.tenant_id).where(
                    unsubscribe_token_directory.c.token_hash == token_hash
                )
            )
        ).scalar_one_or_none()
    if tenant_id is None:
        return None
    async with database.tenant_session(tenant_id, UNSUBSCRIBER) as s:
        row = (
            (await s.execute(select(unsubscribe_tokens).where(unsubscribe_tokens.c.token_hash == token_hash)))
            .mappings()
            .first()
        )
    return (tenant_id, dict(row)) if row else None


@router.get("/{token}")
async def confirm_page(token: str, request: Request) -> HTMLResponse:
    if await _limited(request):
        return _page("Too many requests", "<p>Please try again in a minute.</p>", 429)
    found = await _resolve(request, token)
    if found is None:
        return _invalid()
    if found[1]["used_at"] is not None:
        return _page("You are unsubscribed", "<p>You will not receive these emails any more.</p>")
    action = html.escape(f"/u/{token}", quote=True)
    form = (
        "<p>Stop receiving these emails?</p>"
        f'<form method="post" action="{action}"><input type="hidden" name="List-Unsubscribe" value="One-Click">'
        '<button type="submit">Unsubscribe</button></form>'
    )
    return _page("Unsubscribe", form)


@router.post("/{token}")
async def one_click(token: str, request: Request) -> HTMLResponse:
    if await _limited(request):
        return _page("Too many requests", "<p>Please try again in a minute.</p>", 429)
    found = await _resolve(request, token)
    if found is None:
        return _invalid()
    tenant_id, row = found
    await unsubscribe(request, tenant_id, row)
    return _page("You are unsubscribed", "<p>You will not receive these emails any more.</p>")


async def unsubscribe(request: Request, tenant_id: uuid.UUID, token_row: dict[str, Any]) -> bool:
    """Idempotent: only the first use records the opt-out and publishes the event."""
    database = request.app.state.db
    async with database.tenant_session(tenant_id, UNSUBSCRIBER) as s:
        used = (
            await s.execute(
                update(unsubscribe_tokens)
                .where(unsubscribe_tokens.c.id == token_row["id"], unsubscribe_tokens.c.used_at.is_(None))
                .values(used_at=func.now())
                .returning(unsubscribe_tokens.c.id)
            )
        ).scalar_one_or_none()
        if used is None:
            return False
        message_id = token_row["message_id"]
        await transition(s, tenant_id=tenant_id, message_id=message_id, target="unsubscribed", actor=UNSUBSCRIBER)
        await s.execute(
            update(email_messages)
            .where(email_messages.c.id == message_id, email_messages.c.suppression_reason.is_(None))
            .values(suppression_reason="opt_out", suppression_synced_at=None)
        )
        await emit(
            s,
            "email.unsubscribed",
            tenant_id=tenant_id,
            actor=UNSUBSCRIBER,
            payload={
                "address_hash": token_row["address_hash"],
                "contact_id": str(token_row["contact_id"]) if token_row["contact_id"] else None,
                "message_id": str(message_id),
            },
        )
        await audit(s, UNSUBSCRIBER, "email.unsubscribed", "email_message", message_id, tenant_id=tenant_id,
                    campaign_id=str(token_row["campaign_id"]) if token_row["campaign_id"] else None)  # fmt: skip
        msg = (
            (
                await s.execute(
                    select(
                        email_messages.c.id,
                        email_messages.c.contact_id,
                        email_messages.c.to_address,
                        email_messages.c.suppression_reason,
                    ).where(email_messages.c.id == message_id)  # fmt: skip
                )
            )
            .mappings()
            .one()
        )
    # crm suppression right away when possible; the sync job retries otherwise.
    if msg["suppression_reason"] == "opt_out" and msg["to_address"]:
        crm = service_client(request.app, "crm-service", timeout=3.0)
        try:
            pushed = await push_suppression(crm, tenant_id, msg, "opt_out")
        except Exception:  # noqa: BLE001 - best effort; the sync job retries
            log.warning("unsubscribe_crm_push_failed")
            pushed = False
        if pushed:
            async with database.tenant_session(tenant_id, UNSUBSCRIBER) as s:
                await s.execute(
                    update(email_messages)
                    .where(email_messages.c.id == message_id)
                    .values(suppression_synced_at=func.now())
                )
    return True
