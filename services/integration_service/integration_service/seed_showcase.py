"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): integration METADATA for the console.

* Acme provider connections: HubSpot and Google Calendar (active, OAuth) and an ElevenLabs key in `error`.
* Two platform tenants with one connection each, and platform-level default connections for /platform/integrations.
* Two Acme webhook endpoints (an active CRM/scheduling sync and a disabled legacy export) with a month of
  delivery history built from the showcase bookings, leads and calls: mostly delivered, a few after retries,
  one that failed for good (matching the in-app notification).

Nothing is written to Vault and no row references a secret that exists: every `vault_ref` / `secret_ref` is a
path nobody ever wrote. That is safe for the running stack because
* credentials are resolved only for `active` connections of the provider a service asks for, and the active
  showcase providers (hubspot, google_calendar) are never resolved at runtime here (no booking resource has an
  external calendar; nothing reads HubSpot), while platform defaults are read from Vault directly, not from
  `platform_connections`;
* `secret_registry` gets no rows, so the rotation-reminder and Vault health jobs ignore these connections;
* every delivery is terminal (`succeeded` / `failed`), so the webhook sweeper has nothing to publish. A live event
  would fan out to the active endpoint, but its host (`.example`) never resolves, so it fails closed as
  `unsafe_destination` without any outbound request.

Idempotent (fixed ids, ON CONFLICT DO NOTHING; append-only attempt logs are written only with their delivery).
Runs as `integration_app`, so RLS applies; platform rows are written from a session without tenant context.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from integration_service.tables import (
    integration_tenants,
    platform_connections,
    provider_connections,
    webhook_deliveries,
    webhook_delivery_attempts,
    webhook_endpoints,
)
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, DEMO_USERS
from nexus_core.devshowcase import PLATFORM_TENANTS, Scenario, local, platform_user_id, sid
from nexus_core.vault import platform_secret_path, tenant_secret_path

SEED_ACTOR = Actor("system", "dev-showcase")
ENV = "development"
_USER = {u.email: u.id for u in DEMO_USERS}
ADMIN, PLATFORM_OWNER = _USER["admin@acme.test"], _USER["platform.owner@nexus.local"]
WEBHOOK_ENDPOINT_ID, LEGACY_ENDPOINT_ID = sid("webhook_endpoint", 1), sid("webhook_endpoint", 2)
# The delivery that failed for good ends at 15:42 local, six days before the anchor; the in-app notification in
# notification_service.seed_showcase carries the same time.
FAILED_AT_DAYS_AGO, FAILED_AT = 6, (15, 42)
_RETRY_GAPS = (22, 45, 90, 180, 360)  # seconds between the six attempts (jittered exponential backoff)


def _day(sc: Scenario, days_ago: int, hour: int, minute: int = 0) -> datetime:
    return local(sc.anchor - timedelta(days=days_ago), hour, minute)


def _connection(sc: Scenario, *, n: int, tenant_id: uuid.UUID | None, provider: str, status: str, auth_type: str,
                display_name: str, account: str | None, scopes: list[str], config: dict[str, str],
                created_days_ago: int, created_by: str, rotation_days: int = 90, owner: str | None = None,
                last_error: str | None = None) -> dict[str, Any]:  # fmt: skip
    cid = sid("provider_connection", n)
    created = _day(sc, created_days_ago, 14, 20 + n)
    if tenant_id is None:
        ref = platform_secret_path(ENV, f"showcase/{owner}/{provider}")
    else:
        ref = tenant_secret_path(ENV, str(tenant_id), provider, f"showcase-{cid}")
    ok = status == "active"
    return {
        "id": cid,
        "provider": provider,
        "display_name": display_name,
        "status": status,
        "auth_type": auth_type,
        "vault_ref": ref,
        "vault_version": 1,
        "account_masked": account,
        "granted_scopes": scopes,
        "config": config,
        "last_error": last_error,
        "last_validated_at": _day(sc, 1, 6, n % 60) if ok else None,
        "last_rotated_at": created,
        "next_rotation_due": created + timedelta(days=rotation_days),
        "last_vault_status": None,
        "last_resolved_at": None,
        "created_by": created_by,
        "version": 2 if ok else 3,
        "created_at": created,
        "updated_at": _day(sc, 1, 6, n % 60) if ok else created + timedelta(minutes=2),
        **({"scope": "default"} if tenant_id is not None else {"owning_service": owner}),
    }


def tenant_connections(sc: Scenario) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    admin = f"user:{ADMIN}"
    rows = [
        (ACME.id, _connection(sc, n=1, tenant_id=ACME.id, provider="hubspot", status="active", auth_type="oauth",
                              display_name="HubSpot CRM", account="44…7310",
                              scopes=["crm.objects.contacts.read", "crm.objects.contacts.write", "oauth"], config={},
                              created_days_ago=80, created_by=admin)),
        (ACME.id, _connection(sc, n=2, tenant_id=ACME.id, provider="google_calendar", status="active",
                              auth_type="oauth", display_name="Office calendar", account="o…@acmehome.example",
                              scopes=["https://www.googleapis.com/auth/calendar.events"],
                              config={"calendar_id": "primary"}, created_days_ago=26, created_by=admin)),
        (ACME.id, _connection(sc, n=3, tenant_id=ACME.id, provider="elevenlabs", status="error", auth_type="static",
                              display_name="Custom voice (trial key)", account=None, scopes=[], config={},
                              created_days_ago=11, created_by=admin, last_error="invalid_credentials")),
    ]  # fmt: skip
    brightside, summit = PLATFORM_TENANTS[0], PLATFORM_TENANTS[1]
    rows.append((brightside.id, _connection(
        sc, n=4, tenant_id=brightside.id, provider="google_calendar", status="active", auth_type="oauth",
        display_name="Front desk calendar", account="f…@brightsidedental.example",
        scopes=["https://www.googleapis.com/auth/calendar.events"], config={"calendar_id": "primary"},
        created_days_ago=150, created_by=f"user:{platform_user_id(brightside, 1)}", rotation_days=365)))  # fmt: skip
    rows.append((summit.id, _connection(
        sc, n=5, tenant_id=summit.id, provider="hubspot", status="active", auth_type="oauth",
        display_name="HubSpot (sales pipeline)", account="21…0954",
        scopes=["crm.objects.contacts.read", "crm.objects.deals.read", "oauth"], config={},
        created_days_ago=120, created_by=f"user:{platform_user_id(summit, 0)}", rotation_days=365)))  # fmt: skip
    return rows


def platform_rows(sc: Scenario) -> list[dict[str, Any]]:
    by = f"user:{PLATFORM_OWNER}"
    spec = (  # (owning service, provider, display name, masked account)
        ("call-service", "twilio", "Twilio (platform account)", "AC…9c41"),
        ("voice-service", "deepgram", "Deepgram speech-to-text", "…e7"),
        ("voice-service", "elevenlabs", "ElevenLabs text-to-speech", "…5b"),
        ("email-service", "ses", "Amazon SES (us-east-1)", "AK…Q7XA"),
        ("ai-gateway", "anthropic", "Anthropic API", "sk…f20c"),
        ("ai-gateway", "voyage", "Voyage embeddings", "pa…81d2"),
        ("integration-service", "google_oauth_client", "Google OAuth app", "41…apps"),
        ("integration-service", "hubspot_oauth_client", "HubSpot OAuth app", "7c…e2a1"),
    )
    return [
        _connection(sc, n=20 + k, tenant_id=None, provider=provider, status="active", auth_type="static",
                    display_name=name, account=account, scopes=[], config={}, created_days_ago=220 - 10 * k,
                    created_by=by, rotation_days=365, owner=owner)  # fmt: skip
        for k, (owner, provider, name, account) in enumerate(spec)
    ]


# ------------------------------------------------------------------------------------------- webhooks
def _envelope(event_id: uuid.UUID, event_type: str, at: datetime, producer: str,
              payload: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    return {
        "event_id": str(event_id),
        "event_type": event_type,
        "event_version": 1,
        "occurred_at": at.isoformat(),
        "tenant_id": str(ACME.id),
        "correlation_id": str(sid("webhook_correlation", int(event_id.int % 1_000_000))),
        "causation_id": None,
        "producer": producer,
        "actor": {"type": "service", "id": producer},
        "payload": payload,
    }


def deliveries(sc: Scenario) -> list[dict[str, Any]]:  # noqa: PLR0912 - one branch per event kind
    """Delivery rows (with their attempt log under `attempts`) for both Acme endpoints."""
    rng = random.Random(4_242)  # noqa: S311 - deterministic demo data
    first = local(sc.anchor - timedelta(days=25), 9, 48)  # endpoint created
    legacy_off = _day(sc, 18, 11, 30)
    events: list[tuple[uuid.UUID, str, datetime, str, dict[str, Any]]] = []
    for b in sc.bookings:
        ref = {"booking_id": str(b.id), "contact_id": str(b.contact.id)}
        events.append((WEBHOOK_ENDPOINT_ID, "booking.created", b.created_at + timedelta(seconds=1), "booking-service",
                       {**ref, "status": "confirmed"}))  # fmt: skip
        if b.confirmed_at:
            events.append((WEBHOOK_ENDPOINT_ID, "booking.confirmed", b.confirmed_at + timedelta(seconds=1),
                           "booking-service", {**ref, "status": "confirmed"}))  # fmt: skip
        if b.status == "canceled" and b.canceled_at:
            events.append((WEBHOOK_ENDPOINT_ID, "booking.canceled", b.canceled_at + timedelta(seconds=1),
                           "booking-service", {**ref, "status": "canceled"}))  # fmt: skip
    for c in sc.contacts:
        if c.new_lead:
            events.append((WEBHOOK_ENDPOINT_ID, "lead.created", c.created_at + timedelta(seconds=2), "crm-service",
                           {"contact_id": str(c.id), "lead_id": str(c.lead_id)}))  # fmt: skip
    for call in sc.calls:
        if call.ended_at and call.ended_at < legacy_off:
            events.append((LEGACY_ENDPOINT_ID, "call.completed", call.ended_at + timedelta(seconds=1), "call-service",
                           {"call_id": str(call.id), "direction": call.direction, "final_state": call.final_state,
                            "duration_seconds": call.duration, "contact_id": str(call.contact.id),
                            "campaign_id": str(call.campaign.id) if call.campaign else None,
                            "cost_micros": call.cost_micros}))  # fmt: skip
    events = sorted((e for e in events if e[2] > first), key=lambda e: e[2])
    final = _day(sc, FAILED_AT_DAYS_AGO, *FAILED_AT)
    first_try = final - timedelta(seconds=sum(_RETRY_GAPS))
    failed_n = max(n for n, e in enumerate(events) if e[0] == WEBHOOK_ENDPOINT_ID and e[2] < first_try)
    out: list[dict[str, Any]] = []
    for n, (endpoint, event_type, at, producer, payload) in enumerate(events):
        event_id = sid("webhook_event", n + 1)
        roll = rng.random()
        attempts: list[dict[str, Any]] = []
        t = at + timedelta(seconds=rng.randint(2, 6))
        if n == failed_n:  # the receiving board was down for the whole retry window
            t = first_try
            for k in range(6):
                attempts.append({"outcome": "failed", "status_code": 502, "error": "http_502",
                                 "duration_ms": 0 if k == 5 else rng.randint(180, 900), "at": t})  # fmt: skip
                t += timedelta(seconds=_RETRY_GAPS[k] if k < 5 else 0)
            status = "failed"
        else:
            if roll < 0.08:
                attempts.append({"outcome": "failed", "status_code": 503, "error": "http_503",
                                 "duration_ms": rng.randint(120, 600), "at": t})  # fmt: skip
                t += timedelta(seconds=rng.randint(15, 30))
            elif roll < 0.11:
                attempts.append({"outcome": "failed", "status_code": None, "error": "ReadTimeout",
                                 "duration_ms": 10_000, "at": t})  # fmt: skip
                t += timedelta(seconds=rng.randint(15, 30))
            attempts.append({"outcome": "succeeded", "status_code": 200, "error": None,
                             "duration_ms": rng.randint(70, 420), "at": t})  # fmt: skip
            status = "succeeded"
        last = attempts[-1]
        out.append({
            "n": n + 1,
            "id": sid("webhook_delivery", n + 1),
            "endpoint_id": endpoint,
            "event_id": event_id,
            "event_type": event_type,
            "payload": _envelope(event_id, event_type, at, producer, payload),
            "status": status,
            "attempts_n": len(attempts),
            "next_attempt_at": last["at"],
            "enqueued_at": at + timedelta(seconds=1),
            "last_status_code": last["status_code"],
            "last_error": last["error"],
            "delivered_at": last["at"] if status == "succeeded" else None,
            "created_at": at,
            "updated_at": last["at"] + timedelta(milliseconds=last["duration_ms"]),
            "attempts": attempts,
        })  # fmt: skip
    return out


def endpoints(sc: Scenario, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def last_at(endpoint: uuid.UUID) -> datetime | None:
        times = [r["updated_at"] for r in rows if r["endpoint_id"] == endpoint]
        return max(times) if times else None

    def failures(endpoint: uuid.UUID) -> int:
        streak = 0
        for r in sorted((r for r in rows if r["endpoint_id"] == endpoint), key=lambda r: r["updated_at"]):
            streak = streak + 1 if r["status"] == "failed" else 0
        return streak

    admin = f"user:{ADMIN}"
    created, legacy_created = _day(sc, 25, 9, 48), _day(sc, 60, 13, 5)
    return [
        {"id": WEBHOOK_ENDPOINT_ID, "url": "https://hooks.acmehome.example/nexus/scheduling-board",
         "description": "Push new bookings and leads to the office scheduling board",
         "event_types": ["booking.canceled", "booking.confirmed", "booking.created", "lead.created"],
         "status": "active", "secret_ref": tenant_secret_path(ENV, str(ACME.id), "webhooks", str(WEBHOOK_ENDPOINT_ID)),
         "secret_version": 1, "consecutive_failures": failures(WEBHOOK_ENDPOINT_ID),
         "last_delivery_at": last_at(WEBHOOK_ENDPOINT_ID), "created_by": admin, "version": 1, "created_at": created,
         "updated_at": created},
        {"id": LEGACY_ENDPOINT_ID, "url": "https://hooks.acmehome.example/legacy/call-log",
         "description": "Old call log export (replaced by the HubSpot sync)", "event_types": ["call.completed"],
         "status": "disabled", "secret_ref": tenant_secret_path(ENV, str(ACME.id), "webhooks", str(LEGACY_ENDPOINT_ID)),
         "secret_version": 1, "consecutive_failures": 0, "last_delivery_at": last_at(LEGACY_ENDPOINT_ID),
         "created_by": admin, "version": 2, "created_at": legacy_created, "updated_at": _day(sc, 18, 11, 30)},
    ]  # fmt: skip


async def _write_deliveries(s: AsyncSession, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        values = {k: v for k, v in r.items() if k not in ("attempts", "attempts_n", "n")}
        created = (
            await s.execute(
                insert(webhook_deliveries)
                .values(tenant_id=ACME.id, attempts=r["attempts_n"], **values)
                .on_conflict_do_nothing()
                .returning(webhook_deliveries.c.id)
            )
        ).scalar_one_or_none()
        if created is None:
            continue  # already seeded: the append-only attempt log is never duplicated
        for k, a in enumerate(r["attempts"], start=1):
            await s.execute(
                insert(webhook_delivery_attempts).values(
                    id=sid("webhook_attempt", r["n"] * 10 + k),
                    tenant_id=ACME.id,
                    delivery_id=r["id"],
                    attempt=k,
                    outcome=a["outcome"],
                    status_code=a["status_code"],
                    error=a["error"],
                    duration_ms=a["duration_ms"],
                    created_at=a["at"] + timedelta(milliseconds=a["duration_ms"]),
                )
            )


async def seed(db: Database, sc: Scenario) -> None:
    conns = tenant_connections(sc)
    for tenant_id in dict.fromkeys(t for t, _ in conns):
        async with db.tenant_session(tenant_id, SEED_ACTOR) as s:
            await s.execute(insert(integration_tenants).values(tenant_id=tenant_id).on_conflict_do_nothing())
            for _, row in (c for c in conns if c[0] == tenant_id):
                stmt = insert(provider_connections).values(tenant_id=tenant_id, **row)
                await s.execute(stmt.on_conflict_do_nothing())
            if tenant_id == ACME.id:
                rows = deliveries(sc)
                for ep in endpoints(sc, rows):
                    await s.execute(insert(webhook_endpoints).values(tenant_id=ACME.id, **ep).on_conflict_do_nothing())
                await _write_deliveries(s, rows)
    async with db.global_session(SEED_ACTOR) as s:
        for row in platform_rows(sc):
            await s.execute(insert(platform_connections).values(**row).on_conflict_do_nothing())
