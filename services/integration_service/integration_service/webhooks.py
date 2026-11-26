"""Outbound tenant webhooks (build.md §3.1M): SSRF-guarded, HMAC-signed, retried with backoff, logged.

Flow: a domain event arrives (consumer, deduped by the inbox) → one `webhook_deliveries` row per subscribed
endpoint (unique per endpoint+event) → the sweeper publishes due rows to `integrations.webhooks` → the worker
claims the row atomically and POSTs it:

* the URL is validated with `validate_outbound_url` at registration AND at every delivery, and the request is
  sent to the validated IP (Host/SNI keep the hostname), so DNS rebinding cannot redirect it;
* no redirects, a hard timeout, `X-Nexus-Signature: t=<unix>,v1=<hmac-sha256(secret, "t." + body)>`
  (`nexus_core.webhooks.sign_payload`) with the endpoint secret read from Vault at send time;
* bounded retries with exponential backoff and jitter; when they run out the delivery fails and
  `webhook.delivery.failed` is emitted. Every attempt is recorded in an append-only log.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from integration_service.deps import emit_event, register_tenant
from integration_service.secrets import SecretStore, SecretsUnavailable
from integration_service.settings import Settings
from integration_service.tables import (
    integration_tenants,
    webhook_deliveries,
    webhook_delivery_attempts,
    webhook_endpoints,
)
from nexus_core.celery_app import JobContext
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.errors import Unprocessable
from nexus_core.ssrf import SafeDestination, UnsafeDestination, validate_outbound_url
from nexus_core.webhooks import sign_payload
from nexus_events import EventEnvelope

log = logging.getLogger(__name__)
WORKER = Actor("service", "integration-webhooks")
TASK_NAME = "integrations.deliver_webhook"
QUEUE = "integrations.webhooks"

# event type -> the only producer allowed to emit it (forged events from other producers are ignored)
SUBSCRIBABLE: dict[str, set[str]] = {
    "lead.created": {"crm-service"},
    "call.completed": {"call-service"},
    "booking.created": {"booking-service"},
    "booking.confirmed": {"booking-service"},
    "booking.canceled": {"booking-service"},
    "email.reply.sent": {"email-inbox-service"},
}


def expand_event_types(requested: list[str]) -> list[str]:
    out: set[str] = set()
    for item in requested:
        if item.endswith(".*"):
            matches = {e for e in SUBSCRIBABLE if e.startswith(item[:-1])}
            if not matches:
                raise Unprocessable("Unknown event type pattern", extra={"event_types": sorted(SUBSCRIBABLE)})
            out |= matches
        elif item in SUBSCRIBABLE:
            out.add(item)
        else:
            raise Unprocessable("Unknown event type", extra={"event_types": sorted(SUBSCRIBABLE)})
    return sorted(out)


async def check_destination(url: str) -> SafeDestination:
    """HTTPS only, no credentials in the URL, every resolved address public. Resolution errors fail closed."""
    if len(url) > 2048:
        raise UnsafeDestination("URL too long")
    try:
        return await validate_outbound_url(url)
    except UnsafeDestination:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise UnsafeDestination("Host could not be resolved") from exc


def pinned_url(dest: SafeDestination) -> tuple[str, str]:
    """URL that targets the validated IP; returns (url, original host for Host/SNI)."""
    parts = urlsplit(dest.url)
    ip = dest.addresses[0]
    literal = f"[{ip}]" if isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address) else ip
    netloc = literal if parts.port is None else f"{literal}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, "")), dest.host


def backoff_seconds(settings: Settings, attempts: int) -> float:
    base = settings.webhook_backoff_base_seconds * (2 ** max(attempts - 1, 0))
    return min(base, settings.webhook_backoff_max_seconds) * random.uniform(0.5, 1.0)  # noqa: S311 - jitter


# ------------------------------------------------------------------------------------------- consumers
async def fan_out(event: EventEnvelope, session: AsyncSession) -> None:
    """Event handler (runs in the inbox transaction, tenant RLS context already set): one delivery per
    active subscribed endpoint. Duplicate events never create a second delivery (unique endpoint+event)."""
    if event.tenant_id is None:
        return
    endpoints = (
        await session.execute(
            select(webhook_endpoints.c.id).where(
                webhook_endpoints.c.status == "active", webhook_endpoints.c.event_types.any(event.event_type)
            )
        )
    ).scalars()
    body = json.loads(event.model_dump_json())
    for endpoint_id in endpoints:
        await session.execute(
            insert(webhook_deliveries)
            .values(
                tenant_id=event.tenant_id,
                endpoint_id=endpoint_id,
                event_id=event.event_id,
                event_type=event.event_type,
                payload=body,
            )
            .on_conflict_do_nothing(constraint="uq_webhook_deliveries_dedupe")
        )
    await register_tenant(session, event.tenant_id)


EVENT_HANDLERS = dict.fromkeys(SUBSCRIBABLE, fan_out)


# ------------------------------------------------------------------------------------------- sweeper
Publisher = Callable[[JobContext, str], Awaitable[None]]


async def sweep(database: Database, publish: Publisher, *, batch: int = 100) -> int:
    """Publish due deliveries (and recover rows whose worker died). Idempotent; tasks re-check state."""
    async with database.global_session() as s:
        tenants = list((await s.execute(select(integration_tenants.c.tenant_id))).scalars())
    published = 0
    for tenant_id in tenants:
        async with database.tenant_session(tenant_id, WORKER) as s:
            await s.execute(  # a crashed worker's lease expired: make the row deliverable again
                update(webhook_deliveries)
                .where(webhook_deliveries.c.status == "delivering", webhook_deliveries.c.lease_until < func.now())
                .values(status="queued", enqueued_at=None, lease_until=None, updated_at=func.now())
            )
            due = (
                select(webhook_deliveries.c.id)
                .where(
                    webhook_deliveries.c.status == "queued",
                    webhook_deliveries.c.next_attempt_at <= func.now(),
                    (webhook_deliveries.c.enqueued_at.is_(None))
                    | (webhook_deliveries.c.enqueued_at < func.now() - timedelta(minutes=5)),
                )
                .order_by(webhook_deliveries.c.next_attempt_at)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
            ids = list(
                (
                    await s.execute(
                        update(webhook_deliveries)
                        .where(webhook_deliveries.c.id.in_(due.scalar_subquery()))
                        .values(enqueued_at=func.now())
                        .returning(webhook_deliveries.c.id)
                    )
                ).scalars()
            )
        for delivery_id in ids:  # after commit (ADR-0011 pattern)
            await publish(JobContext(tenant_id=tenant_id, correlation_id=uuid.uuid4(), actor_type="service",
                                     actor_id="integration-webhooks"), str(delivery_id))  # fmt: skip
            published += 1
    return published


# ------------------------------------------------------------------------------------------- delivery
class DeliveryDeps:
    """What the delivery coroutine needs; the worker builds it from `TaskRuntime`, tests build it directly."""

    def __init__(self, database: Database, secrets: SecretStore, settings: Settings,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:  # fmt: skip
        self.db, self.secrets, self.settings, self.transport = database, secrets, settings, transport


async def _record(s: AsyncSession, row: dict[str, Any], attempt: int, outcome: str, *, status_code: int | None,
                  error: str | None, started: float) -> None:  # fmt: skip
    await s.execute(
        insert(webhook_delivery_attempts).values(
            tenant_id=row["tenant_id"],
            delivery_id=row["id"],
            attempt=attempt,
            outcome=outcome,
            status_code=status_code,
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )


async def deliver(deps: DeliveryDeps, ctx: JobContext, delivery_id: uuid.UUID) -> str:
    """Celery task body. Returns the resulting delivery status (for tests and logs)."""
    tenant_id = ctx.tenant_id
    async with deps.db.tenant_session(tenant_id, WORKER) as s:
        claimed = (
            (
                await s.execute(
                    update(webhook_deliveries)
                    .where(
                        webhook_deliveries.c.id == delivery_id,
                        webhook_deliveries.c.status == "queued",
                        webhook_deliveries.c.next_attempt_at <= func.now() + timedelta(seconds=5),
                    )
                    .values(
                        status="delivering",
                        attempts=webhook_deliveries.c.attempts + 1,
                        lease_until=func.now() + timedelta(minutes=2),
                        updated_at=func.now(),
                    )
                    .returning(webhook_deliveries)
                )
            )
            .mappings()
            .first()
        )
        if claimed is None:
            return "skipped"  # duplicate task, not due yet, or already final
        row = dict(claimed)
        endpoint = (
            (await s.execute(select(webhook_endpoints).where(webhook_endpoints.c.id == row["endpoint_id"])))
            .mappings()
            .one()
        )
    started = time.monotonic()
    attempt = row["attempts"]
    if endpoint["status"] != "active":
        return await _finish(deps, row, attempt, "canceled", status_code=None, error="endpoint_inactive",
                             started=started)  # fmt: skip
    try:
        dest = await check_destination(endpoint["url"])
    except UnsafeDestination:
        return await _finish(deps, row, attempt, "failed", status_code=None, error="unsafe_destination",
                             started=started, blocked=True)  # fmt: skip
    try:
        secret = (await deps.secrets.read(endpoint["secret_ref"]))["signing_secret"]
    except (SecretsUnavailable, KeyError):
        return await _retry(deps, row, attempt, error="signing_secret_unavailable", status_code=None, started=started)

    body = json.dumps(row["payload"], separators=(",", ":"), sort_keys=True).encode()
    timestamp = int(time.time())
    signature = sign_payload(secret.get_secret_value().encode(), body, timestamp)
    url, host = pinned_url(dest)
    headers = {
        "Host": host if dest.port in (443, 80) else f"{host}:{dest.port}",
        "Content-Type": "application/json",
        "User-Agent": "NexusCall-Webhooks/1.0",
        "X-Nexus-Signature": signature,
        "X-Nexus-Timestamp": str(timestamp),
        "X-Nexus-Event-Id": str(row["event_id"]),
        "X-Nexus-Event-Type": row["event_type"],
        "X-Nexus-Delivery-Id": str(row["id"]),
    }
    try:
        async with httpx.AsyncClient(timeout=deps.settings.webhook_timeout_seconds, follow_redirects=False,
                                     transport=deps.transport) as client:  # fmt: skip
            resp = await client.post(url, content=body, headers=headers, extensions={"sni_hostname": host})
    except httpx.HTTPError as exc:
        return await _retry(deps, row, attempt, error=type(exc).__name__[:64], status_code=None, started=started)
    if 200 <= resp.status_code < 300:
        return await _finish(deps, row, attempt, "succeeded", status_code=resp.status_code, error=None,
                             started=started)  # fmt: skip
    error = "redirect_not_followed" if 300 <= resp.status_code < 400 else f"http_{resp.status_code}"
    return await _retry(deps, row, attempt, error=error, status_code=resp.status_code, started=started)


async def _retry(deps: DeliveryDeps, row: dict[str, Any], attempt: int, *, error: str, status_code: int | None,
                 started: float) -> str:  # fmt: skip
    if attempt >= deps.settings.webhook_max_attempts:
        return await _finish(deps, row, attempt, "failed", status_code=status_code, error=error, started=started)
    delay = backoff_seconds(deps.settings, attempt)
    async with deps.db.tenant_session(row["tenant_id"], WORKER) as s:
        await s.execute(
            update(webhook_deliveries)
            .where(webhook_deliveries.c.id == row["id"])
            .values(
                status="queued",
                enqueued_at=None,
                lease_until=None,
                next_attempt_at=func.now() + timedelta(seconds=delay),
                last_status_code=status_code,
                last_error=error,
                updated_at=func.now(),
            )
        )
        await _record(s, row, attempt, "failed", status_code=status_code, error=error, started=started)
    return "queued"


async def _finish(deps: DeliveryDeps, row: dict[str, Any], attempt: int, status: str, *, status_code: int | None,
                  error: str | None, started: float, blocked: bool = False) -> str:  # fmt: skip
    async with deps.db.tenant_session(row["tenant_id"], WORKER) as s:
        await s.execute(
            update(webhook_deliveries)
            .where(webhook_deliveries.c.id == row["id"])
            .values(
                status=status,
                lease_until=None,
                last_status_code=status_code,
                last_error=error,
                delivered_at=func.now() if status == "succeeded" else None,
                updated_at=func.now(),
            )
        )
        if status != "canceled":
            outcome = "blocked" if blocked else ("succeeded" if status == "succeeded" else "failed")
            await _record(s, row, attempt, outcome, status_code=status_code, error=error, started=started)
        ep = webhook_endpoints.c
        if status == "succeeded":
            await s.execute(
                update(webhook_endpoints)
                .where(ep.id == row["endpoint_id"])
                .values(consecutive_failures=0, last_delivery_at=func.now())
            )
        elif status == "failed":
            await s.execute(
                update(webhook_endpoints)
                .where(ep.id == row["endpoint_id"])
                .values(consecutive_failures=ep.consecutive_failures + 1, last_delivery_at=func.now())
            )
            await emit_event(
                s,
                "webhook.delivery.failed",
                tenant_id=row["tenant_id"],
                actor=WORKER,
                payload={"endpoint_id": str(row["endpoint_id"]), "delivery_id": str(row["id"]), "attempts": attempt},
            )
    return status
