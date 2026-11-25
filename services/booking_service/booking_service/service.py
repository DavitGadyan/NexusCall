"""Booking operations shared by the internal (tool-execution) and public (UI) APIs.

Guarantees:
* No double booking: the GiST exclusion constraint rejects an overlapping tentative/confirmed appointment on
  the same resource; the violation becomes 409. Concurrent requests serialise on the constraint check.
* Idempotency: every create / confirm / cancel / reschedule runs inside one transaction together with its
  `idempotency_keys` row, so a replay returns the stored response and a failure leaves no trace.
* Ownership: a booking is only ever loaded inside the caller's tenant session, and internal callers must also
  name the booking's contact; anything else is an indistinguishable 404.
* External calendar writes never run inside a request: a `calendar_sync_state` row is queued in the same
  transaction and the sync job applies it idempotently (deterministic event ids, reconcile-first).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from booking_service.deps import audit, booking_event, db, settings_of, ui_refresh
from booking_service.domain import (
    Interval,
    Rule,
    as_utc,
    compute_slots,
    event_id_for,
    fits_rules,
    require_transition,
)
from booking_service.providers.calendar import CalendarError
from booking_service.tables import (
    appointments,
    availability_rules,
    booking_tenants,
    calendar_sync_state,
    idempotency_keys,
    resources,
)
from nexus_core.context import Actor
from nexus_core.credentials import ProviderNotConfigured
from nexus_core.errors import Conflict, NotFound, ServiceUnavailable, Unprocessable
from nexus_core.idempotency import IdempotencyStore

log = logging.getLogger(__name__)
store = IdempotencyStore(idempotency_keys)
NOT_FOUND = "Booking not found"
CANCEL_REASONS = frozenset(
    {"requested", "rescheduled_elsewhere", "no_longer_needed", "duplicate", "staff_unavailable", "other"}
)


# --------------------------------------------------------------------------------------------- helpers
def _is_exclusion_violation(exc: IntegrityError) -> bool:
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        if candidate is None:
            continue
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code == "23P01":
            return True
    return "ex_appointments_no_overlap" in str(orig)


async def register_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await session.execute(insert(booking_tenants).values(tenant_id=tenant_id).on_conflict_do_nothing())


async def tenant_ids(app: FastAPI) -> list[uuid.UUID]:
    async with db(app).global_session() as s:
        return list((await s.execute(select(booking_tenants.c.tenant_id))).scalars())


async def load_rules(session: AsyncSession, resource_id: uuid.UUID) -> list[Rule]:
    rows = (
        (await session.execute(select(availability_rules).where(availability_rules.c.resource_id == resource_id)))
        .mappings()
        .all()
    )
    return [
        Rule(
            weekday=r["weekday"],
            start_time=r["start_time"],
            end_time=r["end_time"],
            slot_minutes=r["slot_minutes"],
            buffer_before_minutes=r["buffer_before_minutes"],
            buffer_after_minutes=r["buffer_after_minutes"],
            min_notice_minutes=r["min_notice_minutes"],
        )
        for r in rows
    ]


async def active_resources(session: AsyncSession, resource_id: uuid.UUID | None) -> list[dict[str, Any]]:
    stmt = select(resources).where(resources.c.status == "active").order_by(resources.c.created_at, resources.c.id)
    if resource_id is not None:
        stmt = stmt.where(resources.c.id == resource_id)
    return [dict(r) for r in (await session.execute(stmt)).mappings().all()]


def check_range(start: datetime, end: datetime, max_days: int) -> tuple[datetime, datetime]:
    start, end = as_utc(start), as_utc(end)
    if end <= start:
        raise Unprocessable("The end must be after the start")
    if end - start > timedelta(days=max_days):
        raise Unprocessable(f"The range may span at most {max_days} days")
    return start, end


# --------------------------------------------------------------------------------------------- calendar
async def external_busy(app: FastAPI, tenant_id: uuid.UUID, resource: dict[str, Any], start: datetime,
                        end: datetime) -> list[Interval]:  # fmt: skip
    """Busy times of the resource's connected calendar. Raises `CalendarError` / `ProviderNotConfigured`."""
    if not resource.get("calendar_provider"):
        return []
    token = await app.state.calendar_tokens.access_token(tenant_id)
    busy = await app.state.calendar.busy(token, resource["calendar_id"], start, end)
    return [Interval(as_utc(b.start), as_utc(b.end)) for b in busy]


async def queue_calendar_sync(
    session: AsyncSession, *, tenant_id: uuid.UUID, appointment: dict[str, Any], resource: dict[str, Any],
    present: bool,
) -> None:  # fmt: skip
    """Record what the external calendar should contain for this appointment (applied by the sync job)."""
    calendar_id = resource.get("calendar_id")
    existing = (
        await session.execute(
            select(calendar_sync_state.c.appointment_id).where(
                calendar_sync_state.c.appointment_id == appointment["id"]
            )
        )
    ).first()
    if calendar_id is None and existing is None:
        return  # no calendar connected and nothing to clean up
    desired = event_id_for(tenant_id, appointment["id"], appointment["version"]) if present and calendar_id else None
    values = {
        "desired_event_id": desired,
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": func.now(),
        "lease_until": None,
        "last_error": None,
        "updated_at": func.now(),
        "resource_id": resource["id"],
    }
    stmt = insert(calendar_sync_state).values(
        tenant_id=tenant_id,
        appointment_id=appointment["id"],
        provider=resource.get("calendar_provider") or "google_calendar",
        calendar_id=calendar_id or "",
        **{k: v for k, v in values.items() if k != "updated_at"},
    )
    update_set: dict[str, Any] = dict(values)
    if calendar_id:
        update_set["calendar_id"] = calendar_id
        update_set["provider"] = resource["calendar_provider"]
    await session.execute(stmt.on_conflict_do_update(index_elements=["tenant_id", "appointment_id"], set_=update_set))


# --------------------------------------------------------------------------------------------- availability
@dataclass(frozen=True, slots=True)
class Slot:
    start: datetime
    end: datetime
    resource_id: uuid.UUID


async def availability(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, start: datetime, end: datetime,
    resource_id: uuid.UUID | None,
) -> tuple[list[Slot], list[uuid.UUID]]:  # fmt: skip
    """Free slots across active resources, minus existing appointments and connected calendars' busy times.

    A resource whose calendar cannot be read offers no slots (fail closed) and is reported as unavailable."""
    cfg = settings_of(app)
    start, end = check_range(start, end, cfg.max_availability_days)
    now = datetime.now(UTC)
    plans: list[tuple[dict[str, Any], list[Rule], list[Interval]]] = []
    async with db(app).tenant_session(tenant_id, actor) as s:
        for res in await active_resources(s, resource_id):
            rules = await load_rules(s, res["id"])
            if not rules:
                continue
            # Pad by the longest buffer so neighbours just outside the range still block padded slots.
            pad = timedelta(minutes=max(max(r.buffer_before_minutes, r.buffer_after_minutes) for r in rules))
            rows = (
                await s.execute(
                    select(appointments.c.start_at, appointments.c.end_at).where(
                        appointments.c.resource_id == res["id"],
                        appointments.c.start_at < end + pad,
                        appointments.c.end_at > start - pad,
                        appointments.c.status.in_(("tentative", "confirmed")),
                        # An expired hold no longer blocks: it is released on the next booking attempt.
                        (appointments.c.status == "confirmed") | (appointments.c.hold_expires_at > func.now()),
                    )
                )
            ).all()
            plans.append((res, rules, [Interval(r.start_at, r.end_at) for r in rows]))
    slots: list[Slot] = []
    unavailable: list[uuid.UUID] = []
    for res, rules, booked in plans:
        try:
            busy = booked + await external_busy(app, tenant_id, res, start, end)
        except (CalendarError, ProviderNotConfigured) as exc:
            log.warning("calendar_busy_unavailable", extra={"resource_id": str(res["id"]), "error": _code(exc)})
            unavailable.append(res["id"])
            continue
        for iv in compute_slots(rules, res["timezone"], start, end, busy, now=now, limit=cfg.max_slots):
            slots.append(Slot(iv.start, iv.end, res["id"]))
    slots.sort(key=lambda x: (x.start, str(x.resource_id)))
    return slots[: cfg.max_slots], unavailable


def _code(exc: Exception) -> str:
    return getattr(exc, "code", None) or type(exc).__name__


# --------------------------------------------------------------------------------------------- idempotency
Handler = Callable[[AsyncSession], Awaitable[tuple[int, dict[str, Any], str | None]]]


async def idempotent(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, *, operation: str, key: str, payload: Any, handler: Handler
) -> tuple[int, dict[str, Any]]:
    """Runs `handler` exactly once per (tenant, operation, key) and replays the stored response afterwards."""
    async with db(app).tenant_session(tenant_id, actor) as s:
        outcome = await store.begin(s, tenant_id=tenant_id, operation=operation, key=key, payload=payload)
        if not outcome.is_new:
            return int(outcome.response_status or 200), dict(outcome.response_body or {})
        status, body, resource_id = await handler(s)
        await store.succeed(s, outcome.record_id, status=status, body=body, resource_id=resource_id)
    return status, body


# --------------------------------------------------------------------------------------------- create
@dataclass(frozen=True, slots=True)
class NewBooking:
    contact_id: uuid.UUID
    start: datetime
    end: datetime
    resource_id: uuid.UUID | None
    status: str  # tentative | confirmed
    source_type: str
    source_id: str | None
    exact_slot: bool  # AI-created holds must match a generated slot exactly


async def _release_expired_holds(
    s: AsyncSession, tenant_id: uuid.UUID, actor: Actor, resource_id: uuid.UUID, start: datetime, end: datetime
) -> None:
    rows = (
        await s.execute(
            update(appointments)
            .where(
                appointments.c.resource_id == resource_id,
                appointments.c.status == "tentative",
                appointments.c.hold_expires_at <= func.now(),
                appointments.c.start_at < end,
                appointments.c.end_at > start,
            )
            .values(
                status="canceled",
                cancel_reason="hold_expired",
                canceled_at=func.now(),
                version=appointments.c.version + 1,
                updated_at=func.now(),
            )
            .returning(appointments.c.id, appointments.c.contact_id)
        )
    ).all()
    for row in rows:
        await booking_event(
            s, "booking.canceled", tenant_id=tenant_id, actor=actor, booking_id=row.id, contact_id=row.contact_id,
            status="canceled",
        )  # fmt: skip


async def _free_candidates(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, req: NewBooking, start: datetime, end: datetime, now: datetime
) -> list[dict[str, Any]]:
    """Resources whose rules admit the slot and whose connected calendar is free. Runs BEFORE the write
    transaction, so no HTTP call ever happens while row locks are held."""
    async with db(app).tenant_session(tenant_id, actor) as s:
        candidates = []
        for res in await active_resources(s, req.resource_id):
            rules = await load_rules(s, res["id"])
            if fits_rules(rules, res["timezone"], start, end, now=now, exact_slot=req.exact_slot):
                candidates.append(res)
    if not candidates:
        if req.resource_id is not None:
            async with db(app).tenant_session(tenant_id, actor) as s:
                if not await active_resources(s, req.resource_id):
                    raise NotFound("Resource not found")
        raise Unprocessable("The requested time is outside the resource's bookable hours")
    free: list[dict[str, Any]] = []
    for res in candidates:
        try:
            busy = await external_busy(app, tenant_id, res, start, end)
        except (CalendarError, ProviderNotConfigured) as exc:
            log.warning("calendar_busy_unavailable", extra={"resource_id": str(res["id"]), "error": _code(exc)})
            if req.resource_id is not None:
                raise ServiceUnavailable("The connected calendar is unavailable; try again shortly") from exc
            continue
        if not any(Interval(start, end).overlaps(b) for b in busy):
            free.append(res)
    if not free:
        raise Conflict("The requested time is no longer available")

    return free


async def create_booking(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, req: NewBooking, *, operation: str, key: str, payload: Any
) -> tuple[int, dict[str, Any]]:
    cfg = settings_of(app)
    start, end = as_utc(req.start), as_utc(req.end)
    if end <= start:
        raise Unprocessable("The end must be after the start")
    if end - start > timedelta(hours=8):
        raise Unprocessable("A booking may last at most 8 hours")
    now = datetime.now(UTC)
    if start <= now:
        raise Unprocessable("A booking must start in the future")
    free = await _free_candidates(app, tenant_id, actor, req, start, end, now)

    async def handler(s: AsyncSession) -> tuple[int, dict[str, Any], str | None]:
        await register_tenant(s, tenant_id)
        hold = now + timedelta(minutes=cfg.tentative_hold_minutes) if req.status == "tentative" else None
        for res in free:
            await _release_expired_holds(s, tenant_id, actor, res["id"], start, end)
            try:
                async with s.begin_nested():
                    row = (
                        (
                            await s.execute(
                                insert(appointments)
                                .values(
                                    tenant_id=tenant_id,
                                    resource_id=res["id"],
                                    contact_id=req.contact_id,
                                    start_at=start,
                                    end_at=end,
                                    status=req.status,
                                    source_type=req.source_type,
                                    source_id=req.source_id,
                                    hold_expires_at=hold,
                                    confirmed_at=func.now() if req.status == "confirmed" else None,
                                    created_by=str(actor)[:140],
                                )
                                .returning(appointments)
                            )
                        )
                        .mappings()
                        .one()
                    )
            except IntegrityError as exc:
                if _is_exclusion_violation(exc):
                    continue  # this resource is taken; try the next candidate
                raise
            appt = dict(row)
            await booking_event(
                s, "booking.created", tenant_id=tenant_id, actor=actor, booking_id=appt["id"],
                contact_id=req.contact_id, status=req.status,
            )  # fmt: skip
            if req.status == "confirmed":
                await booking_event(
                    s, "booking.confirmed", tenant_id=tenant_id, actor=actor, booking_id=appt["id"],
                    contact_id=req.contact_id, status="confirmed",
                )  # fmt: skip
                await queue_calendar_sync(s, tenant_id=tenant_id, appointment=appt, resource=res, present=True)
            await audit(
                s, actor, "booking.created", appt["id"], tenant_id=tenant_id, status=req.status,
                booking_resource_id=str(res["id"]), source_type=req.source_type,
            )  # fmt: skip
            return 201, booking_body(appt), str(appt["id"])
        raise Conflict("The requested time is no longer available")

    status, body = await idempotent(app, tenant_id, actor, operation=operation, key=key, payload=payload,
                                    handler=handler)  # fmt: skip
    await ui_refresh(app, tenant_id, uuid.UUID(body["id"]))
    return status, body


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def booking_body(row: dict[str, Any], sync: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "resource_id": str(row["resource_id"]),
        "contact_id": str(row["contact_id"]),
        "start": _iso(row["start_at"]),
        "end": _iso(row["end_at"]),
        "status": row["status"],
        "source_type": row["source_type"],
        "hold_expires_at": _iso(row["hold_expires_at"]),
        "cancel_reason": row["cancel_reason"],
        "external_event_id": row["external_event_id"],
        "calendar_sync_state": sync["state"] if sync else None,
        "version": row["version"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


# --------------------------------------------------------------------------------------------- transitions
async def _locked(s: AsyncSession, booking_id: uuid.UUID, contact_id: uuid.UUID | None) -> dict[str, Any]:
    stmt = select(appointments).where(appointments.c.id == booking_id).with_for_update()
    row = (await s.execute(stmt)).mappings().first()
    # Wrong tenant (RLS), unknown id and someone else's contact all look the same.
    if row is None or (contact_id is not None and row["contact_id"] != contact_id):
        raise NotFound(NOT_FOUND)
    return dict(row)


async def _resource(s: AsyncSession, resource_id: uuid.UUID) -> dict[str, Any]:
    return dict((await s.execute(select(resources).where(resources.c.id == resource_id))).mappings().one())


async def confirm_booking(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, booking_id: uuid.UUID, *, contact_id: uuid.UUID | None,
    key: str, payload: Any,
) -> tuple[int, dict[str, Any]]:  # fmt: skip
    async def handler(s: AsyncSession) -> tuple[int, dict[str, Any], str | None]:
        appt = await _locked(s, booking_id, contact_id)
        if appt["status"] == "confirmed":
            return 200, booking_body(appt), str(booking_id)
        require_transition(appt["status"], "confirmed")
        if appt["hold_expires_at"] is not None and appt["hold_expires_at"] <= datetime.now(UTC):
            raise Conflict("The tentative hold has expired; book a new slot")
        row = (
            (
                await s.execute(
                    update(appointments)
                    .where(appointments.c.id == booking_id)
                    .values(
                        status="confirmed",
                        hold_expires_at=None,
                        confirmed_at=func.now(),
                        version=appointments.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(appointments)
                )
            )
            .mappings()
            .one()
        )
        appt = dict(row)
        await booking_event(s, "booking.confirmed", tenant_id=tenant_id, actor=actor, booking_id=booking_id,
                            contact_id=appt["contact_id"], status="confirmed")  # fmt: skip
        await queue_calendar_sync(
            s, tenant_id=tenant_id, appointment=appt, resource=await _resource(s, appt["resource_id"]), present=True
        )
        await audit(s, actor, "booking.confirmed", booking_id, tenant_id=tenant_id)
        return 200, booking_body(appt), str(booking_id)

    result = await idempotent(app, tenant_id, actor, operation="booking.confirm", key=key, payload=payload,
                              handler=handler)  # fmt: skip
    await ui_refresh(app, tenant_id, booking_id)
    return result


async def cancel_booking(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, booking_id: uuid.UUID, *, contact_id: uuid.UUID | None,
    reason: str, expected_version: int | None, key: str, payload: Any,
) -> tuple[int, dict[str, Any]]:  # fmt: skip
    code = reason if reason in CANCEL_REASONS else "other"  # free text is never stored (may contain PII)

    async def handler(s: AsyncSession) -> tuple[int, dict[str, Any], str | None]:
        appt = await _locked(s, booking_id, contact_id)
        if expected_version is not None and appt["version"] != expected_version:
            raise Conflict("The booking was changed by someone else; reload and retry")
        if appt["status"] == "canceled":
            return 200, booking_body(appt), str(booking_id)
        require_transition(appt["status"], "canceled")
        was_confirmed = appt["status"] == "confirmed"
        appt = dict(
            (
                await s.execute(
                    update(appointments)
                    .where(appointments.c.id == booking_id)
                    .values(
                        status="canceled",
                        cancel_reason=code,
                        canceled_at=func.now(),
                        version=appointments.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(appointments)
                )
            )
            .mappings()
            .one()
        )
        await booking_event(s, "booking.canceled", tenant_id=tenant_id, actor=actor, booking_id=booking_id,
                            contact_id=appt["contact_id"], status="canceled")  # fmt: skip
        if was_confirmed:
            await queue_calendar_sync(
                s, tenant_id=tenant_id, appointment=appt, resource=await _resource(s, appt["resource_id"]),
                present=False,
            )  # fmt: skip
        await audit(s, actor, "booking.canceled", booking_id, tenant_id=tenant_id, reason=code)
        return 200, booking_body(appt), str(booking_id)

    result = await idempotent(app, tenant_id, actor, operation="booking.cancel", key=key, payload=payload,
                              handler=handler)  # fmt: skip
    await ui_refresh(app, tenant_id, booking_id)
    return result


async def reschedule_booking(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, booking_id: uuid.UUID, *, start: datetime, end: datetime,
    resource_id: uuid.UUID | None, expected_version: int, key: str, payload: Any,
) -> tuple[int, dict[str, Any]]:  # fmt: skip
    start, end = as_utc(start), as_utc(end)
    now = datetime.now(UTC)
    if end <= start or start <= now:
        raise Unprocessable("The new time must be in the future and end after it starts")
    if end - start > timedelta(hours=8):
        raise Unprocessable("A booking may last at most 8 hours")
    async with db(app).tenant_session(tenant_id, actor) as s:
        current = (await s.execute(select(appointments).where(appointments.c.id == booking_id))).mappings().first()
        if current is None:
            raise NotFound(NOT_FOUND)
        target_id = resource_id or current["resource_id"]
        target = await active_resources(s, target_id)
        if not target:
            raise NotFound("Resource not found")
        rules = await load_rules(s, target_id)
    if not fits_rules(rules, target[0]["timezone"], start, end, now=now, exact_slot=False):
        raise Unprocessable("The requested time is outside the resource's bookable hours")
    try:
        busy = await external_busy(app, tenant_id, target[0], start, end)
    except (CalendarError, ProviderNotConfigured) as exc:
        raise ServiceUnavailable("The connected calendar is unavailable; try again shortly") from exc
    if any(Interval(start, end).overlaps(b) for b in busy):
        raise Conflict("The requested time is no longer available")

    async def handler(s: AsyncSession) -> tuple[int, dict[str, Any], str | None]:
        appt = await _locked(s, booking_id, None)
        if appt["version"] != expected_version:
            raise Conflict("The booking was changed by someone else; reload and retry")
        if appt["status"] not in ("tentative", "confirmed"):
            raise Conflict(f"A {appt['status']} booking cannot be rescheduled")
        await _release_expired_holds(s, tenant_id, actor, target_id, start, end)
        try:
            async with s.begin_nested():
                row = (
                    (
                        await s.execute(
                            update(appointments)
                            .where(appointments.c.id == booking_id)
                            .values(
                                start_at=start,
                                end_at=end,
                                resource_id=target_id,
                                version=appointments.c.version + 1,
                                reminder_sent_at=None,
                                updated_at=func.now(),
                            )
                            .returning(appointments)
                        )
                    )
                    .mappings()
                    .one()
                )
        except IntegrityError as exc:
            if _is_exclusion_violation(exc):
                raise Conflict("The requested time is no longer available") from exc
            raise
        new = dict(row)
        if new["status"] == "confirmed":
            await queue_calendar_sync(
                s, tenant_id=tenant_id, appointment=new, resource=await _resource(s, target_id), present=True
            )
        await audit(
            s, actor, "booking.rescheduled", booking_id, tenant_id=tenant_id,
            from_start=_iso(appt["start_at"]), to_start=_iso(start), booking_resource_id=str(target_id),
        )  # fmt: skip
        return 200, booking_body(new), str(booking_id)

    result = await idempotent(app, tenant_id, actor, operation="booking.reschedule", key=key, payload=payload,
                              handler=handler)  # fmt: skip
    await ui_refresh(app, tenant_id, booking_id)
    return result


async def record_outcome(
    app: FastAPI, tenant_id: uuid.UUID, actor: Actor, booking_id: uuid.UUID, outcome: str, expected_version: int
) -> dict[str, Any]:
    async with db(app).tenant_session(tenant_id, actor) as s:
        appt = await _locked(s, booking_id, None)
        if appt["version"] != expected_version:
            raise Conflict("The booking was changed by someone else; reload and retry")
        require_transition(appt["status"], outcome)
        if appt["start_at"] > datetime.now(UTC):
            raise Conflict("An outcome can only be recorded after the appointment has started")
        row = (
            (
                await s.execute(
                    update(appointments)
                    .where(appointments.c.id == booking_id)
                    .values(status=outcome, version=appointments.c.version + 1, updated_at=func.now())
                    .returning(appointments)
                )
            )
            .mappings()
            .one()
        )
        await audit(s, actor, f"booking.{outcome}", booking_id, tenant_id=tenant_id)
    await ui_refresh(app, tenant_id, booking_id)
    return booking_body(dict(row))


# --------------------------------------------------------------------------------------------- periodic jobs
SYSTEM = Actor("system", "booking-jobs")


async def expire_holds(app: FastAPI) -> int:
    """Cancel tentative holds that were not confirmed in time (idempotent, per tenant under RLS)."""
    n = 0
    for tenant_id in await tenant_ids(app):
        async with db(app).tenant_session(tenant_id, SYSTEM) as s:
            rows = (
                await s.execute(
                    update(appointments)
                    .where(appointments.c.status == "tentative", appointments.c.hold_expires_at <= func.now())
                    .values(
                        status="canceled",
                        cancel_reason="hold_expired",
                        canceled_at=func.now(),
                        version=appointments.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(appointments.c.id, appointments.c.contact_id)
                )
            ).all()
            for row in rows:
                n += 1
                await booking_event(s, "booking.canceled", tenant_id=tenant_id, actor=SYSTEM, booking_id=row.id,
                                    contact_id=row.contact_id, status="canceled")  # fmt: skip
                await audit(s, SYSTEM, "booking.hold_expired", row.id, tenant_id=tenant_id)
    return n


async def send_reminders(app: FastAPI) -> int:
    """Request a reminder (via notification-service) once per confirmed appointment inside the lead window."""
    from booking_service.deps import emit_event

    lead = timedelta(minutes=settings_of(app).reminder_lead_minutes)
    n = 0
    for tenant_id in await tenant_ids(app):
        async with db(app).tenant_session(tenant_id, SYSTEM) as s:
            rows = (
                await s.execute(
                    update(appointments)
                    .where(
                        appointments.c.status == "confirmed",
                        appointments.c.reminder_sent_at.is_(None),
                        appointments.c.start_at > func.now(),
                        appointments.c.start_at <= func.now() + lead,
                    )
                    .values(reminder_sent_at=func.now())
                    .returning(appointments.c.id, appointments.c.start_at)
                )
            ).all()
            for row in rows:
                n += 1
                await emit_event(
                    s,
                    "notification.requested",
                    tenant_id=tenant_id,
                    actor=SYSTEM,
                    payload={
                        "template": "booking_reminder",
                        "resource_type": "booking",
                        "resource_id": str(row.id),
                        "channel": "email",
                        "occurred_at": row.start_at.isoformat(),
                    },
                )
    return n


async def sync_calendars(app: FastAPI) -> int:
    """Apply queued calendar changes. Rows are leased, so concurrent runners never process the same row."""
    cfg = settings_of(app)
    done = 0
    for tenant_id in await tenant_ids(app):
        async with db(app).tenant_session(tenant_id, SYSTEM) as s:
            due = (
                select(calendar_sync_state.c.appointment_id)
                .where(
                    ((calendar_sync_state.c.state.in_(("pending", "ambiguous")))
                     & (calendar_sync_state.c.next_attempt_at <= func.now()))
                    | ((calendar_sync_state.c.state == "syncing") & (calendar_sync_state.c.lease_until < func.now()))
                )
                .order_by(calendar_sync_state.c.next_attempt_at)
                .limit(cfg.calendar_sync_batch)
                .with_for_update(skip_locked=True)
            )  # fmt: skip
            claimed = (
                (
                    await s.execute(
                        update(calendar_sync_state)
                        .where(calendar_sync_state.c.appointment_id.in_(due.scalar_subquery()))
                        .values(
                            state="syncing",
                            lease_until=func.now() + text("interval '90 seconds'"),
                            attempts=calendar_sync_state.c.attempts + 1,
                            updated_at=func.now(),
                        )
                        .returning(calendar_sync_state)
                    )
                )
                .mappings()
                .all()
            )
            work = []
            for row in claimed:
                appt = (
                    (await s.execute(select(appointments).where(appointments.c.id == row["appointment_id"])))
                    .mappings()
                    .one()
                )
                work.append((dict(row), dict(appt)))
        for row, appt in work:
            await _sync_one(app, tenant_id, row, appt)
            done += 1
    return done


async def _sync_one(app: FastAPI, tenant_id: uuid.UUID, row: dict[str, Any], appt: dict[str, Any]) -> None:
    from nexus_providers.calendar import CalendarEvent

    cfg = settings_of(app)
    provider = app.state.calendar
    current: str | None = row["current_event_id"]
    current_cal: str | None = row["current_calendar_id"] or row["calendar_id"]
    desired: str | None = row["desired_event_id"]
    error: Exception | None = None
    try:
        token = await app.state.calendar_tokens.access_token(tenant_id)
        if current and current != desired:
            await provider.cancel_event(token, current_cal, current)
            current, current_cal = None, None
        if desired and current != desired:
            # Reconcile first after any earlier attempt: the previous insert may have succeeded silently.
            if row["attempts"] > 1 and await provider.event_exists(token, row["calendar_id"], desired):
                current = desired
            else:
                current = await provider.create_event(
                    token,
                    row["calendar_id"],
                    CalendarEvent(summary="Appointment", start=appt["start_at"], end=appt["end_at"],
                                  idempotency_key=desired),
                )  # fmt: skip
            current_cal = row["calendar_id"]
    except (CalendarError, ProviderNotConfigured) as exc:
        error = exc
    async with db(app).tenant_session(tenant_id, SYSTEM) as s:
        values: dict[str, Any] = {
            "current_event_id": current,
            "current_calendar_id": current_cal,
            "lease_until": None,
            "updated_at": func.now(),
        }
        if error is None:
            values |= {"last_error": None, "last_synced_at": func.now()}
            # A reschedule may have changed the target while we were syncing: then the row is pending again.
            requeued = calendar_sync_state.c.state != "syncing"
            await s.execute(
                update(calendar_sync_state)
                .where(calendar_sync_state.c.appointment_id == row["appointment_id"])
                .values(
                    **values,
                    state=case(
                        (requeued, calendar_sync_state.c.state),
                        (calendar_sync_state.c.desired_event_id.is_not_distinct_from(desired), "synced"),
                        else_="pending",
                    ),
                )
            )
            await s.execute(
                update(appointments).where(appointments.c.id == row["appointment_id"]).values(external_event_id=current)
            )
            return
        permanent = not isinstance(error, _transient_errors()) or row["attempts"] >= cfg.calendar_sync_max_attempts
        backoff = min(30 * 2 ** max(row["attempts"] - 1, 0), 3600)
        log.warning("calendar_sync_failed", extra={"appointment_id": str(row["appointment_id"]),
                                                   "error": _code(error), "attempts": row["attempts"]})  # fmt: skip
        await s.execute(
            update(calendar_sync_state)
            .where(calendar_sync_state.c.appointment_id == row["appointment_id"])
            .values(
                **values,
                state=case(
                    (calendar_sync_state.c.state != "syncing", calendar_sync_state.c.state),
                    else_="failed" if permanent else "ambiguous",
                ),
                last_error=_code(error)[:64],
                next_attempt_at=func.now() + timedelta(seconds=backoff),
            )
        )


def _transient_errors() -> tuple[type[Exception], ...]:
    from booking_service.providers.calendar import CalendarUnavailable

    return (CalendarUnavailable,)
