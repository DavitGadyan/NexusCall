"""The send task (`emails.campaign_send` / `emails.send_message`), as a plain coroutine.

1. Claim the message atomically (`queued` -> `sending`); a duplicate delivery finds nothing to claim.
2. Check again: tenant `effective_emails_enabled`, sender domain verified, suppression (local memory + crm),
   token buckets (provider, per sending domain and stream) and the tenant daily quota.
3. Render (campaign: pinned template version + one-click unsubscribe token; others: stored content).
4. Send through the provider with the message's idempotency key.
5. Record `accepted` (provider acceptance, not inbox delivery).

Permanent problems (suppressed, unverified domain, provider rejection, bad credentials) end in `failed` and
are never retried. Transient problems put the row back to `queued` with bounded exponential backoff + jitter
and re-publish it with a countdown (the sweeper is the safety net). An ambiguous provider error (the request
may have been accepted) leaves the row in `sending` with `ambiguous_at`; only the reconciler decides what
happens next, so a message is never sent twice blindly.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_service.content import render_template
from email_service.deps import Deps
from email_service.messages import load_message, task_for, transition
from email_service.policy import blocks, limiter_for, local_block_reason, seconds_until_next_day, take_daily_quota
from email_service.providers.ses import AmbiguousSendError, to_email_credentials
from email_service.security import UnsafeValue, address_hash, domain_of, new_token
from email_service.tables import (
    email_campaigns,
    email_messages,
    message_directory,
    sender_identities,
    sending_domains,
    template_versions,
    unsubscribe_token_directory,
    unsubscribe_tokens,
)
from nexus_core.celery_app import JobContext
from nexus_core.circuit import CircuitBreaker
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.context import Actor
from nexus_core.credentials import ProviderNotConfigured
from nexus_core.vault import VaultError
from nexus_providers.email import OutboundEmail, PermanentSendError, TransientSendError

log = logging.getLogger(__name__)
SENDER = Actor("system", "email-sender")
MAX_COUNTDOWN = 900.0


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str  # noop | accepted | deferred | failed | ambiguous
    detail: str | None = None
    delay: float | None = None


def backoff(attempt: int, *, base: float, cap: float) -> float:
    return min(cap, base * 2 ** max(0, attempt - 1)) * random.uniform(0.8, 1.2)  # noqa: S311 - jitter only


async def _defer(deps: Deps, ctx: JobContext, msg: dict[str, Any], delay: float, reason: str) -> Outcome:
    tenant_id = ctx.tenant_id
    async with deps.db.tenant_session(tenant_id, SENDER) as s:
        moved = await transition(
            s,
            tenant_id=tenant_id,
            message_id=msg["id"],
            target="queued",
            actor=SENDER,
            only_from=("sending",),
            values={"next_attempt_at": datetime.now(UTC) + timedelta(seconds=delay), "last_error": reason},
        )
    if moved is not None and delay <= MAX_COUNTDOWN:
        await deps.enqueue(
            task_for(msg["category"]),
            _queue(deps, msg["category"]),
            ctx.child(),
            countdown=max(delay, 0.5),
            message_id=str(msg["id"]),
        )
    return Outcome("deferred", reason, delay)


async def _fail(deps: Deps, ctx: JobContext, msg: dict[str, Any], reason: str, *, sources: tuple[str, ...]) -> Outcome:
    async with deps.db.tenant_session(ctx.tenant_id, SENDER) as s:
        await transition(
            s,
            tenant_id=ctx.tenant_id,
            message_id=msg["id"],
            target="failed",
            actor=SENDER,
            only_from=sources,
            values={"last_error": reason[:64]},
        )
    log.info("email_send_failed", extra={"message_id": str(msg["id"]), "reason": reason})
    return Outcome("failed", reason)


def _queue(deps: Deps, category: str) -> str:
    return "emails.campaign_send" if category == "campaign" else deps.settings.transactional_queue


async def _claim(deps: Deps, ctx: JobContext, message_id: uuid.UUID) -> tuple[Outcome | None, dict[str, Any] | None]:
    async with deps.db.tenant_session(ctx.tenant_id, SENDER) as s:
        msg = await load_message(s, message_id, lock=True)
        if msg is None or msg["state"] != "queued":
            return Outcome("noop", "not_queued"), None
        if msg["next_attempt_at"] is not None and msg["next_attempt_at"] > datetime.now(UTC) + timedelta(seconds=1):
            return Outcome("noop", "not_due"), None
        if msg["campaign_id"] is not None:
            state = (
                await s.execute(select(email_campaigns.c.state).where(email_campaigns.c.id == msg["campaign_id"]))
            ).scalar_one_or_none()
            if state in ("completed", "canceled", "failed", None):
                await transition(s, tenant_id=ctx.tenant_id, message_id=message_id, target="failed", actor=SENDER,
                                 values={"last_error": f"campaign_{state or 'missing'}"})  # fmt: skip
                return Outcome("failed", f"campaign_{state or 'missing'}"), None
            if state != "running":
                return Outcome("noop", f"campaign_{state}"), None  # stays queued; picked up after resume
        claimed = await transition(
            s,
            tenant_id=ctx.tenant_id,
            message_id=message_id,
            target="sending",
            actor=SENDER,
            only_from=("queued",),
            values={"claimed_at": func.now(), "next_attempt_at": None, "ambiguous_at": None},
        )
        if claimed is None:
            return Outcome("noop", "not_queued"), None
        sender_domain = (
            await s.execute(
                select(sending_domains.c.status, sending_domains.c.domain).where(
                    sending_domains.c.domain == domain_of(msg["from_address"])
                )
            )
        ).first()
    msg["domain_status"] = sender_domain.status if sender_domain else None
    return None, msg


async def _render(deps: Deps, ctx: JobContext, msg: dict[str, Any]) -> OutboundEmail:
    headers = {"Message-ID": msg["rfc_message_id"]}
    if msg["category"] != "campaign":
        headers.update({k: v for k, v in (msg["extra_headers"] or {}).items() if k in ("In-Reply-To", "References")})
        return OutboundEmail(
            from_address=msg["from_address"],
            from_name=msg["from_name"],
            to_address=msg["to_address"],
            reply_to=msg["reply_to"],
            subject=msg["subject"] or "",
            text=msg["body_text"] or "",
            html=msg["body_html"],
            headers=headers,
            category=msg["category"],
            idempotency_key=msg["idempotency_key"],
        )
    raw, token_hash = new_token()
    async with deps.db.tenant_session(ctx.tenant_id, SENDER) as s:
        version = (
            (
                await s.execute(
                    select(template_versions).where(
                        template_versions.c.template_id == msg["template_id"],
                        template_versions.c.version == msg["template_version"],
                    )
                )
            )
            .mappings()
            .one()
        )
        sender_name = (
            await s.execute(select(sender_identities.c.from_name).where(sender_identities.c.id == msg["sender_id"]))
        ).scalar_one_or_none()
        await s.execute(
            insert(unsubscribe_tokens).values(
                tenant_id=ctx.tenant_id,
                token_hash=token_hash,
                message_id=msg["id"],
                campaign_id=msg["campaign_id"],
                contact_id=msg["contact_id"],
                address_hash=msg["to_hash"],
            )
        )
        await s.execute(insert(unsubscribe_token_directory).values(token_hash=token_hash, tenant_id=ctx.tenant_id))
    url = f"{deps.settings.public_base_url.rstrip('/')}/u/{raw}"
    values = {k: str(v) for k, v in (msg["render_vars"] or {}).items()}
    values["sender_name"] = sender_name or msg["from_name"] or ""
    rendered = render_template(
        category="campaign",
        subject=version["subject"],
        html=version["html"],
        text=version["text"],
        values=values,
        unsubscribe_url=url,
    )
    # RFC 8058 one-click unsubscribe (plus the visible link rendered into the body).
    headers["List-Unsubscribe"] = f"<{url}>"
    headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return OutboundEmail(
        from_address=msg["from_address"],
        from_name=msg["from_name"],
        to_address=msg["to_address"],
        reply_to=msg["reply_to"],
        subject=rendered.subject,
        text=rendered.text,
        html=rendered.html,
        headers=headers,
        category="campaign",
        idempotency_key=msg["idempotency_key"],
    )


async def _checks(deps: Deps, ctx: JobContext, msg: dict[str, Any]) -> Outcome | None:
    s = deps.settings
    tenant_id = ctx.tenant_id
    try:
        comm = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "communication")
    except (UpstreamError, UpstreamUnavailable):
        return await _defer(deps, ctx, msg, 30, "tenant_settings_unavailable")
    if not comm.get("effective_emails_enabled", False):  # tenant or platform kill switch, or tenant inactive
        return await _defer(deps, ctx, msg, s.kill_switch_defer_seconds, "emails_disabled")
    if msg["domain_status"] != "verified":
        return await _fail(deps, ctx, msg, "domain_not_verified", sources=("sending",))
    if not msg["to_address"]:
        return await _fail(deps, ctx, msg, "no_address", sources=("sending",))
    async with deps.db.tenant_session(tenant_id, SENDER) as session:
        local = await local_block_reason(session, msg["to_hash"] or address_hash(msg["to_address"]), msg["category"])
    if local:
        return await _fail(deps, ctx, msg, f"suppressed_{local}", sources=("sending",))
    try:
        check = await deps.client("crm-service").get_json(
            "/internal/v1/suppressions/check",
            params={"channel": "email", "value": msg["to_address"]},
            tenant_id=tenant_id,
            actor=SENDER,
        )
    except (UpstreamError, UpstreamUnavailable):
        return await _defer(deps, ctx, msg, 30, "crm_unavailable")
    if check.get("suppressed") and blocks(msg["category"], check.get("reasons") or ["manual"]):
        return await _fail(deps, ctx, msg, "suppressed", sources=("sending",))
    limiter = limiter_for(deps)
    wait = await limiter.take("email:rate:provider:ses", rate_per_second=s.provider_rate_per_second,
                              burst=s.provider_burst)  # fmt: skip
    if wait > 0:
        return await _defer(deps, ctx, msg, wait, "rate_provider")
    stream = "marketing" if msg["category"] == "campaign" else "transactional"
    rate, burst = (
        (s.domain_rate_per_second, s.domain_burst)
        if stream == "marketing"
        else (s.transactional_domain_rate_per_second, s.transactional_domain_burst)
    )
    wait = await limiter.take(f"email:rate:domain:{tenant_id}:{domain_of(msg['from_address'])}:{stream}",
                              rate_per_second=rate, burst=burst)  # fmt: skip
    if wait > 0:
        return await _defer(deps, ctx, msg, wait, "rate_domain")
    async with deps.db.tenant_session(tenant_id, SENDER) as session:
        within = await take_daily_quota(session, tenant_id, int(comm.get("email_daily_quota", 0) or 0))
    if not within:
        return await _defer(deps, ctx, msg, seconds_until_next_day(), "daily_quota")
    return None


async def send_message(deps: Deps, ctx: JobContext, message_id: uuid.UUID) -> Outcome:
    settings = deps.settings
    stop, msg = await _claim(deps, ctx, message_id)
    if stop is not None or msg is None:
        return stop or Outcome("noop")
    blocked = await _checks(deps, ctx, msg)
    if blocked is not None:
        return blocked
    try:
        outbound = await _render(deps, ctx, msg)
    except (UnsafeValue, ValueError) as exc:
        log.warning("email_render_failed", extra={"message_id": str(message_id), "error_type": type(exc).__name__})
        return await _fail(deps, ctx, msg, "render_failed", sources=("sending",))
    try:
        resolved = await deps.credentials.resolve(ctx.tenant_id, "ses", actor=SENDER, allow_platform_default=True)
        creds = to_email_credentials(resolved)
    except ProviderNotConfigured:
        return await _fail(deps, ctx, msg, "provider_not_configured", sources=("sending",))
    except (UpstreamError, UpstreamUnavailable, VaultError):
        return await _defer(deps, ctx, msg, 60, "credentials_unavailable")
    breaker = CircuitBreaker(deps.redis, "email-provider:ses")
    if await breaker.state() == "open":
        return await _defer(deps, ctx, msg, 30, "provider_circuit_open")
    attempts = int(msg["attempts"]) + 1
    try:
        result = await deps.provider.send(creds, outbound)
    except AmbiguousSendError:
        await breaker.record_failure()
        async with deps.db.tenant_session(ctx.tenant_id, SENDER) as s:
            await s.execute(
                update(email_messages)
                .where(email_messages.c.id == message_id, email_messages.c.state == "sending")
                .values(ambiguous_at=func.now(), attempts=attempts, last_error="ambiguous", updated_at=func.now())
            )
        log.warning("email_send_ambiguous", extra={"message_id": str(message_id)})
        return Outcome("ambiguous", "ambiguous")
    except PermanentSendError as exc:
        await _bump_attempts(deps, ctx, message_id, attempts)
        return await _fail(deps, ctx, msg, f"provider_{exc}"[:64], sources=("sending",))
    except TransientSendError as exc:
        await breaker.record_failure()
        await _bump_attempts(deps, ctx, message_id, attempts)
        if attempts >= settings.send_max_attempts:
            return await _fail(deps, ctx, msg, "retries_exhausted", sources=("sending",))
        delay = backoff(attempts, base=settings.retry_base_seconds, cap=settings.retry_max_seconds)
        return await _defer(deps, ctx, msg, delay, f"provider_{exc}"[:64])
    await breaker.record_success()
    await record_accepted(deps, ctx.tenant_id, message_id, result.provider_message_id, attempts=attempts)
    return Outcome("accepted", result.provider_message_id)


async def _bump_attempts(deps: Deps, ctx: JobContext, message_id: uuid.UUID, attempts: int) -> None:
    async with deps.db.tenant_session(ctx.tenant_id, SENDER) as s:
        await s.execute(update(email_messages).where(email_messages.c.id == message_id).values(attempts=attempts))


async def record_accepted(
    deps: Deps, tenant_id: uuid.UUID, message_id: uuid.UUID, provider_message_id: str, *, attempts: int | None = None
) -> None:
    values: dict[str, Any] = {"provider_message_id": provider_message_id, "last_error": None, "ambiguous_at": None}
    if attempts is not None:
        values["attempts"] = attempts
    async with deps.db.tenant_session(tenant_id, SENDER) as s:
        moved = await transition(
            s, tenant_id=tenant_id, message_id=message_id, target="accepted", actor=SENDER,
            only_from=("sending",), values=values,
        )  # fmt: skip
        if moved is None:  # a provider event already advanced the message; just record the provider id
            await s.execute(
                update(email_messages)
                .where(email_messages.c.id == message_id, email_messages.c.provider_message_id.is_(None))
                .values(provider_message_id=provider_message_id)
            )
        await s.execute(
            update(message_directory)
            .where(message_directory.c.message_id == message_id)
            .values(provider_message_id=provider_message_id)
        )
