"""Public booking API for the UI (`bookings:read` / `bookings:manage`). Create, reschedule and cancel take an
Idempotency-Key and are audited."""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from booking_service import service
from booking_service.deps import audit, db
from booking_service.domain import valid_timezone
from booking_service.tables import appointments, availability_rules, calendar_sync_state, resources
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page

router = APIRouter(prefix="/api/v1", tags=["bookings"])
# Registered BEFORE `router` so `/bookings/resources` is not captured by `/bookings/{booking_id}`.
resources_router = APIRouter(prefix="/api/v1/bookings/resources", tags=["booking-resources"])
Read = Annotated[AuthContext, Depends(require_permission("bookings:read"))]
Manage = Annotated[AuthContext, Depends(require_permission("bookings:manage"))]
IdemKey = Annotated[str | None, Header()]


def _key(value: str | None) -> str:
    if not value:
        raise Unprocessable("Idempotency-Key header is required")
    return value


# ------------------------------------------------------------------------------------------------ models
class BookingOut(BaseModel):
    id: uuid.UUID
    resource_id: uuid.UUID
    contact_id: uuid.UUID
    start: datetime
    end: datetime
    status: str
    source_type: str
    hold_expires_at: datetime | None
    cancel_reason: str | None
    external_event_id: str | None
    calendar_sync_state: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class BookingPage(BaseModel):
    items: list[BookingOut]
    next_cursor: str | None


class CalendarOut(BaseModel):
    items: list[BookingOut]


class BookingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: uuid.UUID
    start: datetime
    end: datetime
    resource_id: uuid.UUID | None = None
    status: Literal["tentative", "confirmed"] = "confirmed"


class RescheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime
    resource_id: uuid.UUID | None = None
    expected_version: int


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["requested", "rescheduled_elsewhere", "no_longer_needed", "duplicate", "staff_unavailable",
                    "other"] = "requested"  # fmt: skip
    expected_version: int | None = None


class OutcomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "no_show"]
    expected_version: int


class SlotOut(BaseModel):
    start: datetime
    end: datetime
    resource_id: uuid.UUID


class AvailabilityOut(BaseModel):
    slots: list[SlotOut]
    calendar_unavailable: list[uuid.UUID]


class ResourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    kind: Literal["calendar", "staff"] = "calendar"
    timezone: str = "UTC"
    calendar_provider: Literal["google_calendar"] | None = None
    calendar_id: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[^\s/?#]+$")
    calendar_connection_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _pair(self) -> ResourceIn:
        if (self.calendar_provider is None) != (self.calendar_id is None):
            raise ValueError("calendar_provider and calendar_id must be set together")
        return self


class ResourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=120)
    timezone: str | None = None
    status: Literal["active", "inactive"] | None = None
    calendar_provider: Literal["google_calendar"] | None = None
    calendar_id: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[^\s/?#]+$")
    calendar_connection_id: uuid.UUID | None = None
    expected_version: int


class ResourceOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    timezone: str
    status: str
    calendar_provider: str | None
    calendar_id: str | None
    calendar_connection_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime


class ResourceList(BaseModel):
    items: list[ResourceOut]


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    slot_minutes: int = Field(30, ge=5, le=480)
    buffer_before_minutes: int = Field(0, ge=0, le=240)
    buffer_after_minutes: int = Field(0, ge=0, le=240)
    min_notice_minutes: int = Field(60, ge=0, le=20160)

    @model_validator(mode="after")
    def _window(self) -> RuleIn:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class RulesPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[RuleIn] = Field(max_length=50)
    expected_version: int


class RuleOut(RuleIn):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID


class RulesOut(BaseModel):
    resource_id: uuid.UUID
    resource_version: int
    rules: list[RuleOut]


# ------------------------------------------------------------------------------------------------ bookings
async def _with_sync(s: Any, rows: list[Any]) -> list[dict[str, Any]]:
    ids = [r["id"] for r in rows]
    states: dict[Any, dict[str, Any]] = {}
    if ids:
        for r in (
            (await s.execute(select(calendar_sync_state).where(calendar_sync_state.c.appointment_id.in_(ids))))
            .mappings()
            .all()
        ):
            states[r["appointment_id"]] = dict(r)
    return [service.booking_body(dict(r), states.get(r["id"])) for r in rows]


@router.get("/bookings", response_model=BookingPage)
async def list_bookings(
    request: Request,
    ctx: Read,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Literal["tentative", "confirmed", "canceled", "completed", "no_show"] | None = None,
    resource_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
) -> dict[str, Any]:
    stmt = select(appointments)
    if status:
        stmt = stmt.where(appointments.c.status == status)
    if resource_id:
        stmt = stmt.where(appointments.c.resource_id == resource_id)
    if contact_id:
        stmt = stmt.where(appointments.c.contact_id == contact_id)
    if from_:
        stmt = stmt.where(appointments.c.end_at > from_)
    if to:
        stmt = stmt.where(appointments.c.start_at < to)
    stmt = apply_keyset(stmt, appointments.c.created_at, appointments.c.id, cursor, limit)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
        page, next_cursor = build_page(rows, limit)
        items = await _with_sync(s, page)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/bookings/calendar", response_model=CalendarOut)
async def calendar_range(
    request: Request,
    ctx: Read,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    resource_id: uuid.UUID | None = None,
    include_canceled: bool = False,
) -> dict[str, Any]:
    """Calendar view: every booking overlapping [from, to), ordered by start (max 62 days, 1000 rows)."""
    start, end = service.check_range(from_, to, 62)
    stmt = select(appointments).where(appointments.c.start_at < end, appointments.c.end_at > start)
    if not include_canceled:
        stmt = stmt.where(appointments.c.status != "canceled")
    if resource_id:
        stmt = stmt.where(appointments.c.resource_id == resource_id)
    stmt = stmt.order_by(appointments.c.start_at, appointments.c.id).limit(1000)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        items = await _with_sync(s, list((await s.execute(stmt)).mappings().all()))
    return {"items": items}


@router.get("/bookings/{booking_id}", response_model=BookingOut)
async def get_booking(booking_id: uuid.UUID, request: Request, ctx: Read) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = (await s.execute(select(appointments).where(appointments.c.id == booking_id))).mappings().first()
        if row is None:
            raise NotFound(service.NOT_FOUND)
        return (await _with_sync(s, [row]))[0]


@router.post("/bookings", response_model=BookingOut, status_code=201)
async def create(body: BookingCreate, request: Request, response: Response, ctx: Manage,
                 idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    req = service.NewBooking(
        contact_id=body.contact_id,
        start=body.start,
        end=body.end,
        resource_id=body.resource_id,
        status=body.status,
        source_type="user",
        source_id=ctx.principal.sub[:128],
        exact_slot=False,
    )
    status, data = await service.create_booking(
        request.app, ctx.require_tenant(), ctx.actor, req, operation="booking.create", key=_key(idempotency_key),
        payload=body.model_dump(mode="json"),
    )  # fmt: skip
    response.status_code = status
    return data


@router.post("/bookings/{booking_id}/reschedule", response_model=BookingOut)
async def reschedule(booking_id: uuid.UUID, body: RescheduleIn, request: Request, response: Response, ctx: Manage,
                     idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    status, data = await service.reschedule_booking(
        request.app, ctx.require_tenant(), ctx.actor, booking_id, start=body.start, end=body.end,
        resource_id=body.resource_id, expected_version=body.expected_version, key=_key(idempotency_key),
        payload={"booking_id": str(booking_id), **body.model_dump(mode="json")},
    )  # fmt: skip
    response.status_code = status
    return data


@router.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
async def cancel(booking_id: uuid.UUID, body: CancelIn, request: Request, response: Response, ctx: Manage,
                 idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    status, data = await service.cancel_booking(
        request.app, ctx.require_tenant(), ctx.actor, booking_id, contact_id=None, reason=body.reason,
        expected_version=body.expected_version, key=_key(idempotency_key),
        payload={"booking_id": str(booking_id), **body.model_dump(mode="json")},
    )  # fmt: skip
    response.status_code = status
    return data


@router.post("/bookings/{booking_id}/outcome", response_model=BookingOut)
async def outcome(booking_id: uuid.UUID, body: OutcomeIn, request: Request, ctx: Manage) -> dict[str, Any]:
    return await service.record_outcome(
        request.app, ctx.require_tenant(), ctx.actor, booking_id, body.status, body.expected_version
    )


@router.get("/availability", response_model=AvailabilityOut)
async def public_availability(
    request: Request,
    ctx: Read,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    resource_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    slots, unavailable = await service.availability(
        request.app, ctx.require_tenant(), ctx.actor, from_, to, resource_id
    )
    return {
        "slots": [{"start": s.start, "end": s.end, "resource_id": s.resource_id} for s in slots],
        "calendar_unavailable": unavailable,
    }


# ------------------------------------------------------------------------------------------------ resources
def _check_tz(name: str | None) -> None:
    if name is not None and not valid_timezone(name):
        raise Unprocessable("Unknown timezone")


@resources_router.get("", response_model=ResourceList)
async def list_resources(request: Request, ctx: Read) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = (await s.execute(select(resources).order_by(resources.c.name))).mappings().all()
    return {"items": [dict(r) for r in rows]}


@resources_router.post("", response_model=ResourceOut, status_code=201)
async def create_resource(body: ResourceIn, request: Request, ctx: Manage) -> dict[str, Any]:
    _check_tz(body.timezone)
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await service.register_tenant(s, tenant_id)
        row = (
            (
                await s.execute(
                    insert(resources)
                    .values(tenant_id=tenant_id, **body.model_dump())
                    .on_conflict_do_nothing(constraint="uq_resources_name")
                    .returning(resources)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("A resource with this name already exists")
        await audit(
            s,
            ctx.actor,
            "booking.resource.created",
            row["id"],
            tenant_id=tenant_id,
            resource_type="booking_resource",
            calendar_connected=body.calendar_provider is not None,
        )
    return dict(row)


@resources_router.patch("/{resource_id}", response_model=ResourceOut)
async def patch_resource(resource_id: uuid.UUID, body: ResourcePatch, request: Request, ctx: Manage) -> dict[str, Any]:
    _check_tz(body.timezone)
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if not changes:
        raise Unprocessable("No changes requested")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        try:
            async with s.begin_nested():
                row = (
                    (
                        await s.execute(
                            update(resources)
                            .where(resources.c.id == resource_id, resources.c.version == body.expected_version)
                            .values(**changes, version=resources.c.version + 1, updated_at=func.now())
                            .returning(resources)
                        )
                    )
                    .mappings()
                    .first()
                )
        except IntegrityError as exc:
            raise Unprocessable("Invalid resource configuration (name taken or calendar fields incomplete)") from exc
        if row is None:
            exists = (await s.execute(select(resources.c.id).where(resources.c.id == resource_id))).first()
            if exists is None:
                raise NotFound("Resource not found")
            raise Conflict("The resource was changed by someone else; reload and retry")
        await audit(s, ctx.actor, "booking.resource.updated", resource_id, tenant_id=tenant_id,
                    resource_type="booking_resource", fields=sorted(changes))  # fmt: skip
    return dict(row)


@resources_router.get("/{resource_id}/rules", response_model=RulesOut)
async def get_rules(resource_id: uuid.UUID, request: Request, ctx: Read) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        version = (await s.execute(select(resources.c.version).where(resources.c.id == resource_id))).scalar()
        if version is None:
            raise NotFound("Resource not found")
        rows = (
            (
                await s.execute(
                    select(availability_rules)
                    .where(availability_rules.c.resource_id == resource_id)
                    .order_by(availability_rules.c.weekday, availability_rules.c.start_time)
                )
            )
            .mappings()
            .all()
        )
    return {"resource_id": resource_id, "resource_version": version, "rules": [dict(r) for r in rows]}


@resources_router.put("/{resource_id}/rules", response_model=RulesOut)
async def put_rules(resource_id: uuid.UUID, body: RulesPut, request: Request, ctx: Manage) -> dict[str, Any]:
    """Replace the weekly rules atomically (optimistic lock on the resource version)."""
    by_day: dict[int, list[RuleIn]] = {}
    for r in body.rules:
        by_day.setdefault(r.weekday, []).append(r)
    for day_rules in by_day.values():
        day_rules.sort(key=lambda r: r.start_time)
        for a, b in itertools.pairwise(day_rules):
            if b.start_time < a.end_time:
                raise Unprocessable("Rules on the same weekday must not overlap")
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        version = (
            await s.execute(
                update(resources)
                .where(resources.c.id == resource_id, resources.c.version == body.expected_version)
                .values(version=resources.c.version + 1, updated_at=func.now())
                .returning(resources.c.version)
            )
        ).scalar()
        if version is None:
            exists = (await s.execute(select(resources.c.id).where(resources.c.id == resource_id))).first()
            if exists is None:
                raise NotFound("Resource not found")
            raise Conflict("The resource was changed by someone else; reload and retry")
        await s.execute(delete(availability_rules).where(availability_rules.c.resource_id == resource_id))
        rows = []
        if body.rules:
            rows = (
                (
                    await s.execute(
                        insert(availability_rules)
                        .values(
                            [{"tenant_id": tenant_id, "resource_id": resource_id, **r.model_dump()} for r in body.rules]
                        )  # fmt: skip
                        .returning(availability_rules)
                    )
                )
                .mappings()
                .all()
            )
        await audit(s, ctx.actor, "booking.rules.replaced", resource_id, tenant_id=tenant_id,
                    resource_type="booking_resource", rules=len(body.rules))  # fmt: skip
    return {"resource_id": resource_id, "resource_version": version, "rules": [dict(r) for r in rows]}
