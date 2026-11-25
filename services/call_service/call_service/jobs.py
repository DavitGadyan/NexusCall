"""Task bodies (plain coroutines; Celery wrappers live in `worker.py`).

* `dispatch_call`        calls.dispatch          claim -> pre-dial gates -> create the provider call
* `reconcile_call`       calls.dispatch          after an ambiguous create: look the call up, retry only if absent
* `process_provider_event` calls.provider_callbacks  apply a verified, deduplicated status callback
* `hangup_call`          calls.dispatch          end a live call at the provider

Invariant: a call row has at most one provider call. A new attempt is made only when every earlier attempt is
known not to exist at the provider (never sent, rejected, or confirmed absent by reconciliation).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from call_service.deps import Deps, twilio_credentials
from call_service.domain import (
    SERVICE_ACTOR,
    after_commit,
    load_call,
    set_check,
    set_provider_call_id,
    slot_key,
    transition,
)
from call_service.gates import PROCEED, GateResult, calling_window_gate, country_gate, next_utc_midnight
from call_service.providers.twilio import NotSent, ProviderCallNotFound, ProviderThrottled
from call_service.state_machine import LIVE, TERMINAL, target_for_provider_status
from call_service.tables import call_attempts, calls, inbound_numbers, provider_events
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.credentials import ProviderNotConfigured
from nexus_core.errors import Conflict, NotFound
from nexus_core.ratelimit import acquire_slot
from nexus_providers.telephony import AmbiguousProviderResult, CreateCallRequest, ProviderRejected

log = logging.getLogger(__name__)
OPEN_ATTEMPT = ("sending", "ambiguous")
RECONCILE_GIVE_UP = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(UTC)


def callback_urls(deps: Deps, call_id: uuid.UUID) -> tuple[str, str]:
    base = deps.settings.webhook_base_url.rstrip("/")
    return f"{base}/webhooks/twilio/status?c={call_id}", f"{base}/webhooks/twilio/answer?c={call_id}"


# ----------------------------------------------------------------------------------------------- dispatch
async def dispatch_call(deps: Deps, ctx: JobContext, call_id: uuid.UUID, *, now: datetime | None = None) -> str:
    now = now or _now()
    tenant_id = ctx.tenant_id
    # 1. Claim atomically: duplicate deliveries of this task find nothing to claim.
    async with deps.db.tenant_session(tenant_id, ctx.actor) as s:
        claimed = (
            (
                await s.execute(
                    select(calls)
                    .where(
                        calls.c.id == call_id,
                        calls.c.state == "queued",
                        (calls.c.scheduled_for.is_(None)) | (calls.c.scheduled_for <= now),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .first()
        )
        if claimed is None:
            return "skipped"
        call = await transition(s, dict(claimed), "initiating", actor=SERVICE_ACTOR)
        assert call is not None
        # If this worker dies mid-way, reconciliation finds an initiating call without an open attempt.
        await set_check(s, call_id, now + timedelta(seconds=deps.settings.reconcile_after_seconds), "reconcile")

    gate, contact = await _run_gates(deps, tenant_id, call, now)
    if gate.verdict != "proceed":
        return await _block(deps, ctx, call_id, gate, now)

    from_e164 = await _caller_id(deps, tenant_id, call)
    if from_e164 is None:
        return await _block(deps, ctx, call_id, GateResult("fail", "no_caller_id"), now)
    to_e164 = str(contact["phone_e164"])

    # 2. Record the attempt BEFORE contacting the provider (crash-safe: reconciliation sees it as open).
    async with deps.db.tenant_session(tenant_id, ctx.actor) as s:
        call = await load_call(s, call_id, lock=True)
        if call["state"] != "initiating":  # canceled meanwhile
            await _release(deps, call)
            return "aborted"
        attempt_no = int(call["attempt_count"]) + 1
        token = f"{call_id}:{attempt_no}"
        await s.execute(
            insert(call_attempts).values(
                tenant_id=tenant_id, call_id=call_id, attempt_no=attempt_no, idempotency_token=token, status="sending"
            )
        )
        await s.execute(
            update(calls)
            .where(calls.c.id == call_id)
            .values(
                attempt_count=attempt_no,
                from_e164=from_e164,
                to_e164=to_e164,
                blocked_reason=None,
                version=calls.c.version + 1,
                updated_at=func.now(),
            )
        )
        await set_check(s, call_id, now + timedelta(seconds=deps.settings.reconcile_after_seconds), "reconcile")

    status_url, answer_url = callback_urls(deps, call_id)
    request = CreateCallRequest(
        to=to_e164,
        from_=from_e164,
        status_callback_url=status_url,
        answer_url=answer_url,
        idempotency_key=token,
        timeout_seconds=deps.settings.ring_timeout_seconds,
    )
    try:
        creds = await twilio_credentials(deps, tenant_id)
        created = await deps.telephony.create_call(creds, request)
    except ProviderNotConfigured:
        await _close_attempt(deps, ctx, call_id, token, "rejected", "provider_not_configured")
        return await _block(deps, ctx, call_id, GateResult("fail", "provider_not_configured"), now)
    except ProviderRejected as exc:
        await _close_attempt(deps, ctx, call_id, token, "rejected", str(exc)[:64] or "rejected")
        return await _block(deps, ctx, call_id, GateResult("fail", "provider_rejected"), now)
    except (NotSent, ProviderThrottled) as exc:
        # Nothing was created: the same row may try again later (bounded by max_dial_attempts).
        await _close_attempt(deps, ctx, call_id, token, "not_found", type(exc).__name__)
        return await _retry_or_fail(deps, ctx, call_id, now, "provider_unavailable")
    except AmbiguousProviderResult:
        # The call may or may not exist. Stay `initiating`; reconciliation decides. Never retry blindly.
        async with deps.db.tenant_session(tenant_id, ctx.actor) as s:
            await s.execute(
                update(call_attempts)
                .where(call_attempts.c.idempotency_token == token, call_attempts.c.status == "sending")
                .values(status="ambiguous", error_code="ambiguous", updated_at=func.now())
            )
            await set_check(s, call_id, now + timedelta(seconds=deps.settings.reconcile_after_seconds), "reconcile")
        return "ambiguous"
    await _record_created(deps, ctx, call_id, token, created.provider_call_id, created.status, now)
    return "dialing"


async def _run_gates(  # noqa: PLR0912 - the six gates read best as one ordered sequence
    deps: Deps, tenant_id: uuid.UUID, call: dict[str, Any], now: datetime
) -> tuple[GateResult, dict[str, Any]]:
    retry = GateResult("reschedule", "dependency_unavailable", now + timedelta(seconds=30))
    # Gate 1: tenant kill switch (platform AND tenant switch AND tenant active).
    try:
        comm = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "communication")
    except (UpstreamError, UpstreamUnavailable):
        return retry, {}
    if not comm.get("effective_calls_enabled", False):
        return GateResult("cancel", "calls_disabled"), {}
    # Gate 2: campaign dispatch state (paused, canceled or killed campaigns never dial).
    if call["campaign_id"] is not None:
        try:
            state = await deps.client("campaign-service").get_json(
                f"/internal/v1/campaigns/{call['campaign_id']}/dispatch-state", tenant_id=tenant_id, actor=SERVICE_ACTOR
            )
        except UpstreamError as exc:
            if exc.is_not_found:
                return GateResult("cancel", "campaign_not_found"), {}
            return retry, {}
        except UpstreamUnavailable:
            return retry, {}
        if not state.get("dispatch_allowed"):
            return GateResult("cancel", f"campaign_{str(state.get('state', 'blocked'))[:24]}"), {}
    # Gate 5 (fetched early: it carries the timezone and number): crm contactability, re-checked right before dialing.
    if call["contact_id"] is None:
        return GateResult("fail", "no_contact"), {}
    try:
        contact = await deps.client("crm-service").get_json(
            f"/internal/v1/contacts/{call['contact_id']}/contactability",
            params={"channel": "phone"},
            tenant_id=tenant_id,
            actor=SERVICE_ACTOR,
        )
    except UpstreamError as exc:
        if exc.is_not_found:
            return GateResult("cancel", "contact_not_found"), {}
        return retry, {}
    except UpstreamUnavailable:
        return retry, {}
    if not contact.get("allowed"):
        reasons = contact.get("reasons") or ["not_contactable"]
        return GateResult("cancel", f"not_contactable:{str(reasons[0])[:32]}"), contact
    # Gate 3: calling window in the CONTACT's timezone (tenant timezone as fallback). Outside it: reschedule.
    tz_name = contact.get("timezone")
    if not tz_name:
        try:
            profile = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "profile")
            tz_name = profile.get("timezone")
        except (UpstreamError, UpstreamUnavailable):
            return retry, contact
    window = calling_window_gate(comm.get("calling_window"), now, tz_name)
    if window.verdict != "proceed":
        return window, contact
    # Gate 4: country allowlist.
    country = country_gate(contact.get("phone_e164"), comm.get("allowed_call_countries"))
    if country.verdict != "proceed":
        return country, contact
    # Gate 6: tenant daily cap and concurrency slot.
    return await _capacity_gate(deps, tenant_id, call, comm, now), contact


async def _capacity_gate(
    deps: Deps, tenant_id: uuid.UUID, call: dict[str, Any], comm: dict[str, Any], now: datetime
) -> GateResult:
    max_daily = int(comm.get("max_daily_calls") or 0)
    max_concurrent = int(comm.get("max_concurrent_calls") or 0)
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    async with deps.db.tenant_session(tenant_id) as s:
        today = (
            await s.execute(
                select(func.count()).select_from(call_attempts).where(call_attempts.c.created_at >= day_start)
            )
        ).scalar_one()
        live = (
            await s.execute(
                select(func.count())
                .select_from(calls)
                .where(
                    calls.c.direction == "outbound",
                    calls.c.id != call["id"],
                    calls.c.state.in_(("ringing", "connected", "in_progress"))
                    | ((calls.c.state == "initiating") & (calls.c.attempt_count > 0)),
                )
            )
        ).scalar_one()
    if max_daily <= 0 or today >= max_daily:
        return GateResult("reschedule", "daily_cap_reached", next_utc_midnight(now))
    retry_at = now + timedelta(seconds=deps.settings.concurrency_retry_seconds)
    if max_concurrent <= 0 or live >= max_concurrent:
        return GateResult("reschedule", "concurrency_limit", retry_at)
    # Atomic slot across workers (the DB count above can race); the lease expires if a holder crashes.
    got = await acquire_slot(
        deps.redis,
        slot_key(tenant_id),
        str(call["id"]),
        max_slots=max_concurrent,
        ttl_seconds=deps.settings.live_call_max_seconds,
    )
    return PROCEED if got else GateResult("reschedule", "concurrency_limit", retry_at)


async def _caller_id(deps: Deps, tenant_id: uuid.UUID, call: dict[str, Any]) -> str | None:
    async with deps.db.tenant_session(tenant_id) as s:
        rows = (
            await s.execute(
                select(inbound_numbers.c.e164, inbound_numbers.c.agent_id)
                .where(inbound_numbers.c.status == "active", inbound_numbers.c.use_for_outbound.is_(True))
                .order_by(inbound_numbers.c.created_at)
            )
        ).all()
    for row in rows:
        if row.agent_id == call["agent_id"]:
            return str(row.e164)
    return str(rows[0].e164) if rows else None


async def _block(deps: Deps, ctx: JobContext, call_id: uuid.UUID, gate: GateResult, now: datetime) -> str:
    """Record why a gate blocked the call: reschedule (scheduled), cancel (canceled) or fail (failed)."""
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        call = await load_call(s, call_id, lock=True)
        if gate.verdict == "reschedule" and gate.retry_at is not None:
            row = await transition(
                s, call, "scheduled", end_reason=gate.reason, scheduled_for=gate.retry_at, actor=SERVICE_ACTOR
            )
            if row is not None:
                await set_check(s, call_id, gate.retry_at, "dispatch")
        else:
            target = "canceled" if gate.verdict == "cancel" else "failed"
            row = await transition(
                s, call, target, end_reason=gate.reason, blocked_reason=gate.reason, actor=SERVICE_ACTOR
            )
    await after_commit(deps.redis, row)
    if gate.verdict != "reschedule":
        await _release(deps, row or call)
    log.info("call_dispatch_blocked", extra={"call_id": str(call_id), "reason": gate.reason, "verdict": gate.verdict})
    return f"{gate.verdict}:{gate.reason}"


async def _release(deps: Deps, call: dict[str, Any]) -> None:
    from nexus_core.ratelimit import release_slot

    try:
        await release_slot(deps.redis, slot_key(call["tenant_id"]), str(call["id"]))
    except Exception:  # noqa: BLE001 - the lease expires on its own
        return


async def _close_attempt(
    deps: Deps, ctx: JobContext, call_id: uuid.UUID, token: str, status: str, error: str | None
) -> None:
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        await s.execute(
            update(call_attempts)
            .where(call_attempts.c.idempotency_token == token)
            .values(status=status, error_code=(error or "")[:64] or None, updated_at=func.now())
        )


async def _retry_or_fail(deps: Deps, ctx: JobContext, call_id: uuid.UUID, now: datetime, reason: str) -> str:
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        call = await load_call(s, call_id, lock=True)
    await _release(deps, call)
    if int(call["attempt_count"]) >= deps.settings.max_dial_attempts:
        return await _block(deps, ctx, call_id, GateResult("fail", reason), now)
    retry_at = now + timedelta(seconds=deps.settings.concurrency_retry_seconds)
    return await _block(deps, ctx, call_id, GateResult("reschedule", reason, retry_at), now)


async def _record_created(
    deps: Deps,
    ctx: JobContext,
    call_id: uuid.UUID,
    token: str,
    provider_call_id: str,
    provider_status: str,
    now: datetime,
) -> None:
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        await s.execute(
            update(call_attempts)
            .where(call_attempts.c.idempotency_token == token)
            .values(status="created", provider_call_id=provider_call_id, updated_at=func.now())
        )
        call = await load_call(s, call_id, lock=True)
        try:
            call = await set_provider_call_id(s, call, provider_call_id)
        except Conflict:
            log.error("call_provider_id_conflict", extra={"call_id": str(call_id)})
            return
        mapped = target_for_provider_status(provider_status)
        row = None
        if mapped and mapped[0] != "initiating":
            row = await transition(s, call, mapped[0], end_reason=mapped[1])
        if (row or call)["state"] not in TERMINAL:
            await set_check(s, call_id, now + timedelta(seconds=deps.settings.live_call_max_seconds), "live")
    await after_commit(deps.redis, row)


# ----------------------------------------------------------------------------------------------- reconcile
async def reconcile_call(deps: Deps, ctx: JobContext, call_id: uuid.UUID, *, now: datetime | None = None) -> str:
    """Resolve an `initiating` call whose provider outcome is unknown, or a live call that went quiet."""
    now = now or _now()
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        call = await load_call(s, call_id)
        attempt = (
            (
                await s.execute(
                    select(call_attempts)
                    .where(call_attempts.c.call_id == call_id)
                    .order_by(call_attempts.c.attempt_no.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
    if call["state"] in TERMINAL:
        return "terminal"
    if call["state"] != "initiating":
        if call["state"] in LIVE and call["provider_call_id"]:
            return await _refresh_live(deps, ctx, call, now)
        return "nothing_to_do"
    if attempt is None or attempt["status"] in ("not_found", "rejected", "superseded"):
        # Claimed but no provider request is outstanding (worker crashed during the gates): safe to re-queue.
        return await _requeue(deps, ctx, call_id, now, "requeued_no_attempt")
    if attempt["status"] == "created":
        return "created"
    age = now - attempt["created_at"]
    if attempt["status"] == "sending" and age < timedelta(seconds=deps.settings.reconcile_after_seconds):
        return "too_early"  # the dispatcher may still be waiting for the provider
    try:
        creds = await twilio_credentials(deps, ctx.tenant_id)
        found = await deps.telephony.find_call(
            creds,
            idempotency_key=attempt["idempotency_token"],
            to=call["to_e164"] or "",
            from_=call["from_e164"] or "",
            created_after=attempt["created_at"],
        )
    except (AmbiguousProviderResult, NotSent, ProviderThrottled, ProviderNotConfigured):
        if age > RECONCILE_GIVE_UP:
            # Still unknown after an hour: give up WITHOUT dialing again (a duplicate call is worse).
            await _close_attempt(deps, ctx, call_id, attempt["idempotency_token"], "ambiguous", "unresolved")
            await _block(deps, ctx, call_id, GateResult("fail", "reconcile_unresolved"), now)
            return "gave_up"
        async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
            await set_check(s, call_id, now + timedelta(seconds=deps.settings.reconcile_after_seconds * 2), "reconcile")
        return "still_ambiguous"
    if found is not None:
        await _record_created(
            deps, ctx, call_id, attempt["idempotency_token"], found.provider_call_id, found.status, now
        )
        return "found"
    # The provider confirms there is no such call: only now may this row try again.
    await _close_attempt(deps, ctx, call_id, attempt["idempotency_token"], "not_found", "confirmed_absent")
    if int(call["attempt_count"]) >= deps.settings.max_dial_attempts:
        await _block(deps, ctx, call_id, GateResult("fail", "provider_unavailable"), now)
        return "failed"
    return await _requeue(deps, ctx, call_id, now, "requeued_after_reconcile")


async def _requeue(deps: Deps, ctx: JobContext, call_id: uuid.UUID, now: datetime, result: str) -> str:
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        call = await load_call(s, call_id, lock=True)
        row = await transition(s, call, "queued", scheduled_for=None)
        if row is None:
            return "not_requeued"
        await set_check(s, call_id, now + timedelta(seconds=deps.settings.stale_queued_seconds), "dispatch")
    await _release(deps, call)
    await deps.enqueue("calls.dispatch", "calls.dispatch", ctx.child(), call_id=str(call_id))
    return result


async def _refresh_live(deps: Deps, ctx: JobContext, call: dict[str, Any], now: datetime) -> str:
    try:
        creds = await twilio_credentials(deps, ctx.tenant_id)
        remote = await deps.telephony.get_call(creds, call["provider_call_id"])
    except ProviderCallNotFound:
        remote = None
    except (AmbiguousProviderResult, NotSent, ProviderNotConfigured):
        async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
            await set_check(s, call["id"], now + timedelta(minutes=5), "live")
        return "unknown"
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        locked = await load_call(s, call["id"], lock=True)
        row = None
        if remote is None:
            row = await transition(s, locked, "failed", end_reason="provider_lost")
        else:
            mapped = target_for_provider_status(remote.status)
            if mapped and mapped[0] in TERMINAL:
                if remote.price_micros:
                    await s.execute(
                        update(calls).where(calls.c.id == call["id"]).values(cost_micros=remote.price_micros)
                    )
                    locked = await load_call(s, call["id"], lock=True)
                row = await transition(
                    s, locked, mapped[0], end_reason=mapped[1], duration_seconds=remote.duration_seconds
                )
        if row is None:
            await set_check(s, call["id"], now + timedelta(minutes=10), "live")
    await after_commit(deps.redis, row)
    return "refreshed" if row else "still_live"


# ----------------------------------------------------------------------------------------------- callbacks
async def process_provider_event(deps: Deps, ctx: JobContext, event_id: uuid.UUID) -> str:
    """Apply one verified provider event. Duplicates never get here twice (unique (provider, event_id)), and a
    second delivery of this task finds the event already processed."""
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        event = (
            (
                await s.execute(
                    select(provider_events)
                    .where(provider_events.c.id == event_id, provider_events.c.status == "received")
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .first()
        )
        if event is None:
            return "skipped"
        outcome, row = await _apply_status(s, dict(event))
        await s.execute(
            update(provider_events)
            .where(provider_events.c.id == event_id)
            .values(status="processed" if outcome == "applied" else "ignored", processed_at=func.now())
        )
    await after_commit(deps.redis, row)
    return outcome


async def _apply_status(s: Any, event: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    if event["call_id"] is None:
        return "ignored_no_call", None
    try:
        call = await load_call(s, event["call_id"], lock=True)
    except NotFound:
        return "ignored_no_call", None
    data = event["data"] or {}
    sid = data.get("call_sid")
    if sid and call["provider_call_id"] is None:
        try:
            call = await set_provider_call_id(s, call, sid)
        except Conflict:
            return "ignored_mismatch", None
    elif sid and call["provider_call_id"] != sid:
        return "ignored_mismatch", None
    seq = event["sequence"]
    last = call["last_provider_sequence"]
    if seq is not None and last is not None and seq <= last:
        return "ignored_out_of_order", None
    extra: dict[str, Any] = {"last_provider_status": event["provider_status"]}
    if seq is not None:
        extra["last_provider_sequence"] = seq
    price = data.get("price_micros")
    if isinstance(price, int) and price >= 0:
        extra["cost_micros"] = price
    await s.execute(update(calls).where(calls.c.id == call["id"]).values(**extra))
    call = await load_call(s, call["id"], lock=True)
    mapped = target_for_provider_status(str(event["provider_status"] or ""))
    if mapped is None or mapped[0] == call["state"]:
        return "ignored_no_change", None
    duration = data.get("duration_seconds")
    row = await transition(
        s,
        call,
        mapped[0],
        end_reason=mapped[1],
        duration_seconds=int(duration) if isinstance(duration, int) else None,
    )
    return ("applied", row) if row is not None else ("ignored_illegal", None)


# ----------------------------------------------------------------------------------------------- hangup
async def hangup_call(deps: Deps, ctx: JobContext, call_id: uuid.UUID) -> str:
    async with deps.db.tenant_session(ctx.tenant_id, ctx.actor) as s:
        call = await load_call(s, call_id)
    if call["state"] in TERMINAL or not call["provider_call_id"]:
        return "nothing_to_do"
    status = "canceled" if call["state"] in ("initiating", "ringing") else "completed"
    try:
        creds = await twilio_credentials(deps, ctx.tenant_id)
        await deps.telephony.hangup(creds, call["provider_call_id"], status=status)
    except ProviderRejected:
        return "rejected"  # already ended at the provider; the status callback finishes the row
    except (AmbiguousProviderResult, NotSent, ProviderThrottled) as exc:
        raise TransientFailure("hangup_unconfirmed") from exc
    return "requested"
