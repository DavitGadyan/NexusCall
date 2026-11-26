"""Internal API for tool-execution (BUILD_CONVENTIONS §5, booking-service).

The tenant and actor come from the verified service call (`require_internal`). The contact comes from the
caller's verified conversation context, never from model arguments, and every booking is checked against it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from booking_service import service
from booking_service.settings import get_settings
from nexus_core.authz import InternalContext, require_internal
from nexus_core.errors import Unprocessable

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
internal_guard = require_internal(*get_settings().internal_client_allowlist)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=32, pattern=r"^[a-z_]+$")
    id: str = Field(min_length=1, max_length=128)


class TentativeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: uuid.UUID
    start: datetime
    end: datetime
    resource_id: uuid.UUID | None = None
    source: Source


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: uuid.UUID | None = None
    source: Source | None = None


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=200)
    contact_id: uuid.UUID | None = None
    source: Source | None = None


class SlotOut(BaseModel):
    start: datetime
    end: datetime
    resource_id: uuid.UUID


class AvailabilityOut(BaseModel):
    slots: list[SlotOut]


class TentativeOut(BaseModel):
    booking_id: uuid.UUID
    status: str
    start: datetime
    end: datetime


class StatusOut(BaseModel):
    booking_id: uuid.UUID
    status: str


def _key(value: str | None) -> str:
    if not value:
        raise Unprocessable("Idempotency-Key header is required")
    return value


SOURCE_TYPES = {"ai_tool": "ai_tool", "call": "call", "user": "user"}


@router.get("/availability", response_model=AvailabilityOut)
async def availability(
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    resource_id: uuid.UUID | None = None,
    ctx: InternalContext = Depends(internal_guard),
) -> dict[str, Any]:
    slots, _ = await service.availability(request.app, ctx.require_tenant(), ctx.actor, from_, to, resource_id)
    return {"slots": [{"start": s.start, "end": s.end, "resource_id": s.resource_id} for s in slots]}


@router.post("/bookings/tentative", response_model=TentativeOut, status_code=201)
async def tentative(
    body: TentativeIn,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None),
    ctx: InternalContext = Depends(internal_guard),
) -> dict[str, Any]:
    req = service.NewBooking(
        contact_id=body.contact_id,
        start=body.start,
        end=body.end,
        resource_id=body.resource_id,
        status="tentative",
        source_type=SOURCE_TYPES.get(body.source.type, "api"),
        source_id=body.source.id,
        exact_slot=True,
    )
    status, data = await service.create_booking(
        request.app, ctx.require_tenant(), ctx.actor, req, operation="booking.tentative",
        key=_key(idempotency_key), payload=body.model_dump(mode="json"),
    )  # fmt: skip
    response.status_code = status
    return {"booking_id": data["id"], "status": data["status"], "start": data["start"], "end": data["end"]}


@router.post("/bookings/{booking_id}/confirm", response_model=StatusOut)
async def confirm(
    booking_id: uuid.UUID,
    request: Request,
    response: Response,
    body: ConfirmIn | None = None,
    idempotency_key: str | None = Header(default=None),
    ctx: InternalContext = Depends(internal_guard),
) -> dict[str, Any]:
    body = body or ConfirmIn()
    if body.contact_id is None:
        # Every internal caller acts inside a conversation: the booking must belong to its contact.
        raise Unprocessable("contact_id is required")
    status, data = await service.confirm_booking(
        request.app, ctx.require_tenant(), ctx.actor, booking_id, contact_id=body.contact_id,
        key=_key(idempotency_key), payload={"booking_id": str(booking_id), **body.model_dump(mode="json")},
    )  # fmt: skip
    response.status_code = status
    return {"booking_id": data["id"], "status": data["status"]}


@router.post("/bookings/{booking_id}/cancel", response_model=StatusOut)
async def cancel(
    booking_id: uuid.UUID,
    body: CancelIn,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None),
    ctx: InternalContext = Depends(internal_guard),
) -> dict[str, Any]:
    status, data = await service.cancel_booking(
        request.app, ctx.require_tenant(), ctx.actor, booking_id, contact_id=body.contact_id,
        reason=body.reason, expected_version=None, key=_key(idempotency_key),
        payload={"booking_id": str(booking_id), **body.model_dump(mode="json")},
    )  # fmt: skip
    response.status_code = status
    return {"booking_id": data["id"], "status": data["status"]}
