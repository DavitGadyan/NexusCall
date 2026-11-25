"""Periodic jobs (ADR-0014: Redis-locked, single runner per tick). They select rows and publish ids; the
actual sending runs in the worker.

* `scheduler_tick`: for live campaigns, start scheduled ones, page the crm segment into `pending` rows
  (unique per campaign and contact), then promote controlled batches to `queued` after a crm `contacts/batch`
  check, and publish `emails.campaign_send`. Campaigns complete when every recipient is final.
* `sweeper_tick`: re-publishes `queued` rows that were not picked up and reconciles stale `sending` rows
  (ambiguous provider errors, crashed workers): ask the provider when it can answer, never resend blindly.
* `suppression_sync_tick`: pushes hard bounces, complaints and opt-outs to crm `POST /internal/v1/suppressions`.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from email_service.deps import Deps, audit
from email_service.messages import register_message, task_for, transition
from email_service.providers.ses import to_email_credentials
from email_service.security import address_hash, domain_of, new_rfc_message_id, valid_address
from email_service.sending import record_accepted
from email_service.tables import (
    campaign_schedule_index,
    email_campaigns,
    email_messages,
    sender_identities,
    tenant_activity,
)
from nexus_core.celery_app import JobContext
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.context import Actor
from nexus_core.credentials import ProviderNotConfigured
from nexus_providers.email import TransientSendError

log = logging.getLogger(__name__)
SCHEDULER = Actor("system", "email-scheduler")
SWEEPER = Actor("system", "email-sweeper")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def in_window(window: dict[str, Any] | None, tz: str, now: datetime) -> bool:
    if not window:
        return True
    local = now.astimezone(ZoneInfo(tz))
    hhmm = local.strftime("%H:%M")
    return DAYS[local.weekday()] in window.get("days", DAYS) and window.get("start", "00:00") <= hhmm < window.get(
        "end", "23:59"
    )


def _ctx(tenant_id: uuid.UUID, actor: Actor) -> JobContext:
    return JobContext(tenant_id=tenant_id, correlation_id=uuid.uuid4(), actor_type=actor.type, actor_id=actor.id)


async def active_tenants(deps: Deps) -> list[uuid.UUID]:
    since = datetime.now(UTC) - timedelta(days=deps.settings.active_tenant_days)
    async with deps.db.global_session(SWEEPER) as s:
        return list(
            (await s.execute(select(tenant_activity.c.tenant_id).where(tenant_activity.c.last_activity_at >= since)))
            .scalars()
            .all()
        )


# ------------------------------------------------------------------------------------------------ scheduler
async def scheduler_tick(deps: Deps, *, now: datetime | None = None) -> int:
    async with deps.db.global_session(SCHEDULER) as s:
        live = (
            await s.execute(select(campaign_schedule_index.c.tenant_id, campaign_schedule_index.c.campaign_id))
        ).all()
    published = 0
    for tenant_id, campaign_id in live:
        try:
            published += await run_campaign(deps, tenant_id, campaign_id, now=now)
        except Exception:
            log.exception("email_campaign_tick_failed", extra={"campaign_id": str(campaign_id)})
    return published


async def _unindex(deps: Deps, campaign_id: uuid.UUID) -> None:
    async with deps.db.global_session(SCHEDULER) as s:
        await s.execute(campaign_schedule_index.delete().where(campaign_schedule_index.c.campaign_id == campaign_id))


async def _finish(deps: Deps, tenant_id: uuid.UUID, camp: dict[str, Any], state: str, reason: str) -> None:
    async with deps.db.tenant_session(tenant_id, SCHEDULER) as s:
        done = await s.execute(
            update(email_campaigns)
            .where(email_campaigns.c.id == camp["id"], email_campaigns.c.state == camp["state"])
            .values(
                state=state,
                completed_at=func.now(),
                last_error=None if state == "completed" else reason,
                version=email_campaigns.c.version + 1,
                updated_at=func.now(),
            )  # fmt: skip
        )
        if done.rowcount:  # type: ignore[attr-defined]
            await s.execute(
                update(email_messages)
                .where(email_messages.c.campaign_id == camp["id"], email_messages.c.state.in_(("pending", "queued")))
                .values(state="failed", last_error=reason, state_changed_at=func.now(), updated_at=func.now())
            )
            await audit(s, SCHEDULER, f"email_campaign.{state}", "email_campaign", camp["id"], tenant_id=tenant_id,
                        reason=reason)  # fmt: skip
    await _unindex(deps, camp["id"])


async def run_campaign(deps: Deps, tenant_id: uuid.UUID, campaign_id: uuid.UUID, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    async with deps.db.tenant_session(tenant_id, SCHEDULER) as s:
        row = (await s.execute(select(email_campaigns).where(email_campaigns.c.id == campaign_id))).mappings().first()
    camp = dict(row) if row else None
    if camp is None or camp["state"] in ("completed", "canceled", "failed", "draft"):
        await _unindex(deps, campaign_id)
        return 0
    if camp["state"] == "paused":
        return 0
    if camp["state"] == "scheduled":
        if camp["scheduled_at"] and camp["scheduled_at"] > now:
            return 0
        async with deps.db.tenant_session(tenant_id, SCHEDULER) as s:
            started = await s.execute(
                update(email_campaigns)
                .where(email_campaigns.c.id == campaign_id, email_campaigns.c.state == "scheduled")
                .values(
                    state="running", started_at=func.now(), version=email_campaigns.c.version + 1, updated_at=func.now()
                )  # fmt: skip
            )
            if started.rowcount:  # type: ignore[attr-defined]
                await audit(s, SCHEDULER, "email_campaign.started", "email_campaign", campaign_id, tenant_id=tenant_id)
        camp["state"] = "running"
    if camp["ends_at"] and camp["ends_at"] <= now:
        await _finish(deps, tenant_id, camp, "completed", "campaign_ended")
        return 0
    try:
        comm = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "communication")
    except (UpstreamError, UpstreamUnavailable):
        return 0
    if not comm.get("effective_emails_enabled", False):
        return 0  # kill switch: hold (nothing new is queued; the worker also re-checks before every send)
    if not in_window(comm.get("email_window"), camp["timezone"], now):
        return 0
    if not camp["snapshot_done"] and not await _snapshot_page(deps, tenant_id, camp):
        return 0
    published = await _promote(deps, tenant_id, camp)  # -1: work still in flight, 0: nothing left to queue
    if published == 0 and camp["snapshot_done"]:
        await _maybe_complete(deps, tenant_id, campaign_id)
    return max(published, 0)


async def _snapshot_page(deps: Deps, tenant_id: uuid.UUID, camp: dict[str, Any]) -> bool:
    """One crm page per tick into `pending` rows. Returns False when the tick should stop here."""
    try:
        page = await deps.client("crm-service").post_json(
            f"/internal/v1/segments/{camp['segment_id']}/resolve",
            {"cursor": camp["snapshot_cursor"], "limit": deps.settings.snapshot_page_size, "channel": "email"},
            tenant_id=tenant_id,
            actor=SCHEDULER,
        )
    except UpstreamError as exc:
        if exc.is_not_found:
            await _finish(deps, tenant_id, camp, "failed", "segment_not_found")
        return False
    except UpstreamUnavailable:
        return False
    contact_ids = [uuid.UUID(str(i["contact_id"])) for i in page.get("items", [])]
    nxt = page.get("next_cursor")
    async with deps.db.tenant_session(tenant_id, SCHEDULER) as s:
        sender = (
            (await s.execute(select(sender_identities).where(sender_identities.c.id == camp["sender_id"])))
            .mappings()
            .one()
        )
        if contact_ids:
            rows = []
            for contact_id in contact_ids:
                message_id = uuid.uuid4()
                rows.append(
                    {
                        "id": message_id,
                        "tenant_id": tenant_id,
                        "category": "campaign",
                        "state": "pending",
                        "campaign_id": camp["id"],
                        "contact_id": contact_id,
                        "sender_id": sender["id"],
                        "template_id": camp["template_id"],
                        "template_version": camp["template_version"],
                        "from_address": sender["from_address"],
                        "from_name": sender["from_name"],
                        "reply_to": sender["reply_to"],
                        "rfc_message_id": new_rfc_message_id(domain_of(sender["from_address"])),
                        "idempotency_key": str(message_id),
                    }
                )
            created = (
                await s.execute(
                    insert(email_messages)
                    .values(rows)
                    .on_conflict_do_nothing(
                        index_elements=["tenant_id", "campaign_id", "contact_id"],
                        index_where=email_messages.c.campaign_id.is_not(None),
                    )
                    .returning(email_messages.c.id)
                )
            ).scalars()
            for message_id in created:
                await register_message(s, tenant_id, message_id)
            await s.execute(
                insert(tenant_activity)
                .values(tenant_id=tenant_id)
                .on_conflict_do_update(index_elements=["tenant_id"], set_={"last_activity_at": func.now()})
            )
        await s.execute(
            update(email_campaigns)
            .where(email_campaigns.c.id == camp["id"])
            .values(snapshot_cursor=nxt, snapshot_done=nxt is None, updated_at=func.now())
        )
    camp["snapshot_cursor"], camp["snapshot_done"] = nxt, nxt is None
    return True


async def _promote(deps: Deps, tenant_id: uuid.UUID, camp: dict[str, Any]) -> int:
    """Move a controlled batch pending -> queued (with the address and render values from crm) and publish."""
    settings = deps.settings
    allowance = max(1, math.ceil(camp["max_per_minute"] * settings.scheduler_interval_seconds / 60))
    allowance = min(allowance, settings.scheduler_batch_max)
    async with deps.db.tenant_session(tenant_id, SCHEDULER) as s:
        inflight = (
            await s.execute(
                select(func.count()).where(
                    email_messages.c.campaign_id == camp["id"], email_messages.c.state.in_(("queued", "sending"))
                )
            )
        ).scalar_one()
    budget = min(allowance, max(0, allowance * 2 - int(inflight)))
    if budget == 0:
        return -1  # still busy: not complete
    async with deps.db.tenant_session(tenant_id, SCHEDULER) as s:
        pending = (
            await s.execute(
                select(email_messages.c.id, email_messages.c.contact_id)
                .where(email_messages.c.campaign_id == camp["id"], email_messages.c.state == "pending")
                .order_by(email_messages.c.created_at, email_messages.c.id)
                .limit(budget)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not pending:
            return -1 if inflight else 0
        try:
            batch = await deps.client("crm-service").post_json(
                "/internal/v1/contacts/batch",
                {"contact_ids": [str(p.contact_id) for p in pending], "channel": "email"},
                tenant_id=tenant_id,
                actor=SCHEDULER,
            )
        except (UpstreamError, UpstreamUnavailable):
            return -1
        found = {str(i["contact_id"]): i for i in batch.get("items", [])}
        queued: list[uuid.UUID] = []
        for message_id, contact_id in pending:
            item = found.get(str(contact_id))  # crm omits unknown ids
            email = (item or {}).get("email")
            if item is None or not item.get("allowed") or not email or not valid_address(str(email)):
                reason = "contact_not_found" if item is None else ((item.get("reasons") or ["no_address"])[0])
                await transition(s, tenant_id=tenant_id, message_id=message_id, target="failed", actor=SCHEDULER,
                                 values={"last_error": f"skipped_{reason}"[:64]})  # fmt: skip
                continue
            first, last = item.get("first_name") or "", item.get("last_name") or ""
            moved = await transition(
                s,
                tenant_id=tenant_id,
                message_id=message_id,
                target="queued",
                actor=SCHEDULER,
                only_from=("pending",),
                values={
                    "to_address": str(email),
                    "to_hash": address_hash(str(email)),
                    "render_vars": {"first_name": first, "last_name": last, "full_name": f"{first} {last}".strip()},
                },
            )
            if moved is not None:
                queued.append(message_id)
    for message_id in queued:
        await deps.enqueue(
            task_for("campaign"), "emails.campaign_send", _ctx(tenant_id, SCHEDULER), message_id=str(message_id)
        )
    return len(queued) or -1


async def _maybe_complete(deps: Deps, tenant_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
    async with deps.db.tenant_session(tenant_id, SCHEDULER) as s:
        camp = (await s.execute(select(email_campaigns).where(email_campaigns.c.id == campaign_id))).mappings().one()
        if camp["state"] != "running" or not camp["snapshot_done"]:
            return
        open_count = (
            await s.execute(
                select(func.count()).where(
                    email_messages.c.campaign_id == campaign_id,
                    email_messages.c.state.in_(("pending", "queued", "sending")),
                )
            )
        ).scalar_one()
    if open_count == 0:
        await _finish(deps, tenant_id, dict(camp), "completed", "all_recipients_final")


# --------------------------------------------------------------------------------------------------- sweeper
async def sweeper_tick(deps: Deps, *, now: datetime | None = None) -> dict[str, int]:
    totals = {"republished": 0, "reconciled": 0, "requeued": 0, "failed": 0}
    for tenant_id in await active_tenants(deps):
        try:
            for key, n in (await sweep_tenant(deps, tenant_id, now=now)).items():
                totals[key] += n
        except Exception:
            log.exception("email_sweep_failed", extra={"tenant": str(tenant_id)})
    return totals


async def sweep_tenant(deps: Deps, tenant_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, int]:
    settings = deps.settings
    now = now or datetime.now(UTC)
    out = {"republished": 0, "reconciled": 0, "requeued": 0, "failed": 0}
    stale_queued = now - timedelta(seconds=settings.stuck_queued_seconds)
    async with deps.db.tenant_session(tenant_id, SWEEPER) as s:
        live_campaign = select(email_campaigns.c.id).where(email_campaigns.c.state == "running")
        rows = (
            await s.execute(
                update(email_messages)
                .where(
                    email_messages.c.id.in_(
                        select(email_messages.c.id)
                        .where(
                            email_messages.c.state == "queued",
                            email_messages.c.updated_at < stale_queued,
                            or_(email_messages.c.next_attempt_at.is_(None), email_messages.c.next_attempt_at <= now),
                            or_(
                                email_messages.c.campaign_id.is_(None), email_messages.c.campaign_id.in_(live_campaign)
                            ),
                        )
                        .limit(500)
                        .with_for_update(skip_locked=True)
                    )
                )
                .values(updated_at=func.now())
                .returning(email_messages.c.id, email_messages.c.category)
            )
        ).all()
    for message_id, category in rows:
        queue = "emails.campaign_send" if category == "campaign" else settings.transactional_queue
        await deps.enqueue(task_for(category), queue, _ctx(tenant_id, SWEEPER), message_id=str(message_id))
        out["republished"] += 1
    stale_sending = now - timedelta(seconds=settings.stuck_sending_seconds)
    async with deps.db.tenant_session(tenant_id, SWEEPER) as s:
        stuck = (
            (
                await s.execute(
                    select(
                        email_messages.c.id,
                        email_messages.c.category,
                        email_messages.c.idempotency_key,
                        email_messages.c.claimed_at,
                        email_messages.c.ambiguous_at,
                    )  # fmt: skip
                    .where(
                        email_messages.c.state == "sending",
                        or_(
                            email_messages.c.claimed_at < stale_sending,
                            and_(email_messages.c.claimed_at.is_(None), email_messages.c.updated_at < stale_sending),
                        ),
                    )
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
    for msg in stuck:
        result = await reconcile(deps, tenant_id, dict(msg), now=now)
        out[result] = out.get(result, 0) + 1
    return out


async def reconcile(deps: Deps, tenant_id: uuid.UUID, msg: dict[str, Any], *, now: datetime) -> str:
    """Decide what happened to a stale `sending` message without risking a duplicate send."""
    provider = deps.provider
    if provider.supports_lookup:
        try:
            resolved = await deps.credentials.resolve(tenant_id, "ses", actor=SWEEPER, allow_platform_default=True)
            provider_id = await provider.lookup(to_email_credentials(resolved), msg["idempotency_key"])
        except (TransientSendError, UpstreamError, UpstreamUnavailable, ProviderNotConfigured):
            return "skipped"
        if provider_id is not None:
            await record_accepted(deps, tenant_id, msg["id"], provider_id)
            return "reconciled"
        # Authoritative "never accepted": safe to send again with the same idempotency key.
        async with deps.db.tenant_session(tenant_id, SWEEPER) as s:
            moved = await transition(s, tenant_id=tenant_id, message_id=msg["id"], target="queued", actor=SWEEPER,
                                     only_from=("sending",),
                                     values={"ambiguous_at": None, "next_attempt_at": None,
                                             "last_error": "reconciled_not_sent"})  # fmt: skip
        if moved is not None:
            queue = "emails.campaign_send" if msg["category"] == "campaign" else deps.settings.transactional_queue
            await deps.enqueue(task_for(msg["category"]), queue, _ctx(tenant_id, SWEEPER), message_id=str(msg["id"]))
        return "requeued"
    # The provider cannot answer (real SES): wait for its event stream, then give up without resending.
    since = msg["ambiguous_at"] or msg["claimed_at"] or now
    if now - since < timedelta(seconds=deps.settings.ambiguous_give_up_seconds):
        return "skipped"
    async with deps.db.tenant_session(tenant_id, SWEEPER) as s:
        await transition(s, tenant_id=tenant_id, message_id=msg["id"], target="failed", actor=SWEEPER,
                         only_from=("sending",), values={"last_error": "ambiguous_unconfirmed"})  # fmt: skip
    return "failed"


# ------------------------------------------------------------------------------------------ suppression sync
async def suppression_sync_tick(deps: Deps) -> int:
    synced = 0
    for tenant_id in await active_tenants(deps):
        try:
            synced += await sync_tenant_suppressions(deps, tenant_id)
        except Exception:
            log.exception("email_suppression_sync_failed", extra={"tenant": str(tenant_id)})
    return synced


async def sync_tenant_suppressions(deps: Deps, tenant_id: uuid.UUID, *, limit: int = 200) -> int:
    async with deps.db.tenant_session(tenant_id, SWEEPER) as s:
        rows = (
            (
                await s.execute(
                    select(
                        email_messages.c.id,
                        email_messages.c.contact_id,
                        email_messages.c.to_address,
                        email_messages.c.suppression_reason,
                    )  # fmt: skip
                    .where(
                        email_messages.c.suppression_reason.is_not(None),
                        email_messages.c.suppression_synced_at.is_(None),
                    )
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
    crm = deps.client("crm-service")
    done = 0
    for row in rows:
        reason = {"bounce": "bounce", "complaint": "complaint", "opt_out": "opt_out"}[row["suppression_reason"]]
        if await push_suppression(crm, tenant_id, row, reason):
            async with deps.db.tenant_session(tenant_id, SWEEPER) as s:
                await s.execute(
                    update(email_messages)
                    .where(email_messages.c.id == row["id"])
                    .values(suppression_synced_at=func.now())
                )
            done += 1
        else:
            break  # crm unavailable: try again next tick
    return done


async def push_suppression(crm: Any, tenant_id: uuid.UUID, row: Any, reason: str) -> bool:
    """True when crm recorded (or already had) the suppression, or when the request can never succeed."""
    body: dict[str, Any] = {
        "channel": "email",
        "value": row["to_address"],
        "reason": reason,
        "source": {"type": "email_message", "id": str(row["id"])},
    }
    if row["contact_id"]:
        body["contact_id"] = str(row["contact_id"])
    try:
        await crm.post_json(
            "/internal/v1/suppressions", body, tenant_id=tenant_id, actor=SWEEPER,
            idempotency_key=f"email-suppression:{row['id']}",
        )  # fmt: skip
        return True
    except UpstreamUnavailable:
        return False
    except UpstreamError as exc:
        if exc.upstream_status >= 500 or exc.upstream_status == 429:
            return False
        if row["contact_id"] and exc.upstream_status in (404, 422):  # contact gone: suppress by address only
            body.pop("contact_id")
            try:
                await crm.post_json(
                    "/internal/v1/suppressions", body, tenant_id=tenant_id, actor=SWEEPER,
                    idempotency_key=f"email-suppression:{row['id']}:address",
                )  # fmt: skip
            except UpstreamUnavailable:
                return False
            except UpstreamError:
                log.warning("email_suppression_rejected", extra={"message_id": str(row["id"])})
        return True
