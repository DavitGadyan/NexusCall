"""Campaign dispatcher: a Redis-locked PeriodicJob (ADR-0014) that paces calls for every running campaign.

Per tick and campaign (inside that tenant's RLS session):
  1. Gates: tenant `effective_calls_enabled`, tenant kill switch, campaign state/start/end, calling window,
     the calls-per-minute token bucket (persisted on the campaign row), in-flight vs max concurrency (campaign
     and tenant), and the tenant daily cap.
  2. Claim due contacts with `FOR UPDATE SKIP LOCKED` and mark them `in_flight` (commit).
  3. Re-check contactability in crm, then `POST call-service /internal/v1/calls` with the idempotency key
     `campaign:{id}:contact:{contact_id}:attempt:{n}`.
A refusal as not contactable marks the contact `skipped`. When call-service is unreachable the attempt is put
back with the SAME attempt number, so the retry reuses the same idempotency key (no duplicate call).
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_service.domain import (
    Deps,
    complete_if_done,
    kill_switch_engaged,
    record,
    transition,
)
from campaign_service.tables import call_campaigns, campaign_contacts, campaign_events, dispatch_registry
from nexus_core.celery_app import JobContext
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.context import Actor

log = logging.getLogger(__name__)
DISPATCHER = Actor("system", "campaign-dispatcher")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
NOT_CONTACTABLE_CODES = frozenset({"not_contactable", "contact_not_contactable", "suppressed", "no_consent"})


def bucket_capacity(calls_per_minute: int) -> int:
    """Small burst (≈5 s of budget) so any 60 s window stays close to `calls_per_minute`."""
    return max(1, math.ceil(calls_per_minute / 12))


def refill(tokens: float | None, refilled_at: datetime | None, calls_per_minute: int, now: datetime) -> float:
    capacity = bucket_capacity(calls_per_minute)
    if tokens is None or refilled_at is None:
        return float(capacity)
    elapsed = max(0.0, (now - refilled_at).total_seconds())
    return min(float(capacity), tokens + elapsed * calls_per_minute / 60.0)


def in_window(campaign: dict[str, Any], now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(campaign["timezone"]))
    hhmm = local.strftime("%H:%M")
    return (
        DAYS[local.weekday()] in campaign["window_days"] and campaign["window_start"] <= hhmm < campaign["window_end"]
    )


def _day_start(campaign: dict[str, Any], now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(campaign["timezone"]))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def _count(session: AsyncSession, *where: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(campaign_contacts).where(*where))).scalar_one())


async def _end_campaign(session: AsyncSession, campaign: dict[str, Any], reason: str) -> None:
    await session.execute(
        update(campaign_contacts)
        .where(
            campaign_contacts.c.campaign_id == campaign["id"], campaign_contacts.c.state.in_(("pending", "retry_wait"))
        )
        .values(state="skipped", skip_reason=reason, version=campaign_contacts.c.version + 1, updated_at=func.now())
    )
    await transition(
        session, campaign, "complete", actor=DISPATCHER, values={"completed_at": func.now()}, reason=reason
    )


async def claim(
    deps: Deps, tenant_id: uuid.UUID, campaign_id: uuid.UUID, comm: dict[str, Any], now: datetime, batch_max: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Gate checks + claim in one transaction. Returns (campaign, claimed contacts)."""
    async with deps.db.tenant_session(tenant_id, DISPATCHER) as s:
        if await kill_switch_engaged(s):
            return None, []
        row = (
            (
                await s.execute(
                    select(call_campaigns).where(call_campaigns.c.id == campaign_id).with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["state"] != "running":
            return None, []
        c = dict(row)
        if c["end_at"] is not None and now >= c["end_at"]:
            await _end_campaign(s, c, "campaign_ended")
            return None, []
        if c["start_at"] is not None and now < c["start_at"]:
            return None, []
        if await complete_if_done(s, c, DISPATCHER):
            return None, []
        if not in_window(c, now):
            return None, []
        tokens = refill(c["pacing_tokens"], c["pacing_refilled_at"], c["calls_per_minute"], now)
        in_flight = await _count(
            s, campaign_contacts.c.campaign_id == campaign_id, campaign_contacts.c.state == "in_flight"
        )
        tenant_in_flight = await _count(s, campaign_contacts.c.state == "in_flight")
        dispatched_today = int(
            (
                await s.execute(
                    select(func.count()).where(
                        campaign_events.c.kind == "call_dispatched", campaign_events.c.created_at >= _day_start(c, now)
                    )
                )
            ).scalar_one()
        )
        room = min(
            math.floor(tokens),
            c["max_concurrency"] - in_flight,
            int(comm.get("max_concurrent_calls", 0)) - tenant_in_flight,
            int(comm.get("max_daily_calls", 0)) - dispatched_today,
            batch_max,
        )
        claimed: list[dict[str, Any]] = []
        if room > 0:
            due = (
                (
                    await s.execute(
                        select(campaign_contacts)
                        .where(
                            campaign_contacts.c.campaign_id == campaign_id,
                            (campaign_contacts.c.state == "pending")
                            | (
                                (campaign_contacts.c.state == "retry_wait")
                                & (campaign_contacts.c.next_attempt_at <= now)
                            ),
                        )
                        .order_by(
                            campaign_contacts.c.next_attempt_at.asc().nulls_first(), campaign_contacts.c.created_at
                        )
                        .limit(room)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            for cc in due:
                updated = (
                    (
                        await s.execute(
                            update(campaign_contacts)
                            .where(campaign_contacts.c.id == cc["id"])
                            .values(
                                state="in_flight",
                                attempts=campaign_contacts.c.attempts + 1,
                                dispatched_at=now,
                                last_call_id=None,
                                next_attempt_at=None,
                                version=campaign_contacts.c.version + 1,
                                updated_at=func.now(),
                            )
                            .returning(campaign_contacts)
                        )
                    )
                    .mappings()
                    .one()
                )
                claimed.append(dict(updated))
                await record(
                    s,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    kind="call_dispatched",
                    actor=DISPATCHER,
                    contact_id=cc["contact_id"],
                    data={"attempt": int(updated["attempts"])},
                )
            tokens -= len(claimed)
        await s.execute(
            update(call_campaigns)
            .where(call_campaigns.c.id == campaign_id)
            .values(pacing_tokens=tokens, pacing_refilled_at=now)
        )
        return c, claimed


async def _apply(
    deps: Deps,
    tenant_id: uuid.UUID,
    results: list[tuple[dict[str, Any], str, dict[str, Any]]],
    retry_seconds: int,
    now: datetime,
) -> None:
    async with deps.db.tenant_session(tenant_id, DISPATCHER) as s:
        for cc, outcome, extra in results:
            guard = (
                campaign_contacts.c.id == cc["id"],
                campaign_contacts.c.state == "in_flight",
                campaign_contacts.c.attempts == cc["attempts"],
            )
            if outcome == "dialing":
                values: dict[str, Any] = {"last_call_id": extra["call_id"]}
            elif outcome == "skipped":
                values = {"state": "skipped", "skip_reason": extra["reason"][:64], "completed_at": now}
            elif outcome == "failed":
                values = {"state": "failed", "outcome": extra["reason"][:64], "completed_at": now}
            else:  # transient: same attempt number again later → same idempotency key
                values = {
                    "state": "retry_wait",
                    "attempts": cc["attempts"] - 1,
                    "next_attempt_at": now + timedelta(seconds=retry_seconds),
                }
            await s.execute(
                update(campaign_contacts)
                .where(*guard)
                .values(**values, version=campaign_contacts.c.version + 1, updated_at=func.now())
            )


async def dispatch_campaign(
    deps: Deps,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    comm: dict[str, Any],
    *,
    now: datetime | None = None,
    batch_max: int = 50,
    retry_seconds: int = 30,
) -> int:
    """Returns the number of calls handed to call-service in this tick."""
    now = now or datetime.now(UTC)
    campaign, claimed = await claim(deps, tenant_id, campaign_id, comm, now, batch_max)
    if campaign is None or not claimed:
        return 0
    results: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    try:
        check = await deps.client("crm-service").post_json(
            "/internal/v1/contacts/batch",
            {"contact_ids": [str(cc["contact_id"]) for cc in claimed], "channel": "phone"},
            tenant_id=tenant_id,
            actor=DISPATCHER,
        )
        verdicts = {uuid.UUID(str(i["contact_id"])): i for i in check.get("items", [])}
    except (UpstreamError, UpstreamUnavailable):
        log.warning("dispatch_contactability_unavailable", extra={"campaign_id": str(campaign_id)})
        await _apply(deps, tenant_id, [(cc, "transient", {}) for cc in claimed], retry_seconds, now)
        return 0
    calls = deps.client("call-service")
    sent = 0
    for cc in claimed:
        verdict = verdicts.get(cc["contact_id"])
        if verdict is None or not verdict.get("allowed"):
            reasons = (verdict or {}).get("reasons") or ["contact_not_found"]
            results.append((cc, "skipped", {"reason": f"not_contactable:{reasons[0]}"}))
            continue
        key = f"campaign:{campaign_id}:contact:{cc['contact_id']}:attempt:{cc['attempts']}"
        try:
            created = await calls.post_json(
                "/internal/v1/calls",
                {
                    "contact_id": str(cc["contact_id"]),
                    "agent_id": str(campaign["agent_id"]),
                    "campaign_id": str(campaign_id),
                    "campaign_contact_id": str(cc["id"]),
                },
                tenant_id=tenant_id,
                actor=DISPATCHER,
                idempotency_key=key,
            )
            results.append((cc, "dialing", {"call_id": uuid.UUID(str(created["call_id"]))}))
            sent += 1
        except UpstreamUnavailable:
            results.append((cc, "transient", {}))
        except UpstreamError as exc:
            if exc.upstream_status >= 500 or exc.upstream_status == 429:
                results.append((cc, "transient", {}))
            elif exc.upstream_status == 422 or exc.upstream_code in NOT_CONTACTABLE_CODES:
                results.append((cc, "skipped", {"reason": exc.upstream_code or "not_contactable"}))
            else:
                results.append((cc, "failed", {"reason": f"rejected_{exc.upstream_status}"}))
    await _apply(deps, tenant_id, results, retry_seconds, now)
    return sent


async def dispatch_tick(
    deps: Deps,
    *,
    now: datetime | None = None,
    batch_max: int = 50,
    retry_seconds: int = 30,
    snapshot_stale_seconds: int = 300,
    enqueue: Any = None,
    tenant_ids: set[uuid.UUID] | None = None,
) -> int:
    """One dispatcher pass over every running campaign (optionally only some tenants: operations tooling and
    tests). Also re-enqueues stalled audience snapshots."""
    now = now or datetime.now(UTC)
    async with deps.db.global_session(DISPATCHER) as s:
        rows = (
            await s.execute(
                select(
                    dispatch_registry.c.campaign_id,
                    dispatch_registry.c.tenant_id,
                    dispatch_registry.c.state,
                    dispatch_registry.c.snapshot_status,
                    dispatch_registry.c.updated_at,
                )
            )
        ).all()
    if tenant_ids is not None:
        rows = [r for r in rows if r.tenant_id in tenant_ids]
    stale = now - timedelta(seconds=snapshot_stale_seconds)
    if enqueue is not None:
        for r in rows:
            if r.snapshot_status in ("pending", "running") and r.updated_at < stale:
                ctx = JobContext(
                    tenant_id=r.tenant_id,
                    correlation_id=uuid.uuid4(),
                    actor_type="system",
                    actor_id="campaign-dispatcher",
                )
                try:
                    await enqueue("campaigns.snapshot", "calls.schedule", ctx, campaign_id=str(r.campaign_id))
                except Exception:  # noqa: BLE001 - next tick tries again
                    log.warning("snapshot_requeue_failed", extra={"campaign_id": str(r.campaign_id)})
    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for r in rows:
        if r.state == "running":
            by_tenant[r.tenant_id].append(r.campaign_id)
    total = 0
    for tenant_id, campaign_ids in by_tenant.items():
        try:
            comm = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "communication")
        except (UpstreamError, UpstreamUnavailable):
            log.warning("dispatch_settings_unavailable", extra={"tenant_id": str(tenant_id)})
            continue  # fail closed: no settings, no calls
        if not comm.get("effective_calls_enabled", False):
            continue
        for campaign_id in campaign_ids:
            try:
                total += await dispatch_campaign(
                    deps, tenant_id, campaign_id, comm, now=now, batch_max=batch_max, retry_seconds=retry_seconds
                )
            except Exception:
                log.exception("dispatch_campaign_failed", extra={"campaign_id": str(campaign_id)})
    return total
