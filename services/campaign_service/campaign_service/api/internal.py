"""Service-to-service API, exactly as BUILD_CONVENTIONS §5 (campaign-service)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from campaign_service.domain import kill_switch_engaged, load_campaign
from nexus_core.authz import InternalContext, require_internal

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
CallService = Annotated[InternalContext, Depends(require_internal("svc-call-service"))]


class DispatchState(BaseModel):
    campaign_id: uuid.UUID
    state: str
    dispatch_allowed: bool


@router.get("/campaigns/{campaign_id}/dispatch-state", response_model=DispatchState)
async def dispatch_state(campaign_id: uuid.UUID, request: Request, ictx: CallService) -> DispatchState:
    """call-service checks this right before dialing: paused, canceled or killed campaigns never dial."""
    now = datetime.now(UTC)
    async with request.app.state.db.tenant_session(ictx.require_tenant(), ictx.actor) as s:
        c = await load_campaign(s, campaign_id)
        killed = await kill_switch_engaged(s)
    allowed = (
        c["state"] == "running"
        and not killed
        and (c["start_at"] is None or c["start_at"] <= now)
        and (c["end_at"] is None or now < c["end_at"])
    )
    return DispatchState(campaign_id=campaign_id, state=c["state"], dispatch_allowed=allowed)
