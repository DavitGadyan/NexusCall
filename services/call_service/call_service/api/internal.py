"""Service-to-service API, exactly as BUILD_CONVENTIONS §5 (call-service), plus voice-service session hooks.

Tenant and actor come from the verified internal headers; every call is loaded inside that tenant's RLS
session, so a wrong tenant finds nothing (404).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from call_service.deps import AppDeps, twilio_credentials
from call_service.domain import after_commit, audit, emit, job_ctx, load_call, register_directory, transition
from call_service.providers.twilio import NotSent, ProviderThrottled
from call_service.schemas import (
    AckOut,
    CancelOut,
    CreateCallIn,
    CreateCallOut,
    InternalCallOut,
    NotContactable,
    OptOutOut,
    ReasonIn,
    TransferOut,
)
from call_service.settings import get_settings
from call_service.state_machine import ANSWERED, LIVE, PENDING
from call_service.tables import calls, idempotency_keys, inbound_numbers
from nexus_core.authz import InternalContext, require_internal
from nexus_core.clients import UpstreamError, UpstreamUnavailable
from nexus_core.credentials import ProviderNotConfigured
from nexus_core.errors import ServiceUnavailable, Unprocessable
from nexus_core.idempotency import IdempotencyStore
from nexus_providers.telephony import AmbiguousProviderResult, ProviderRejected

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
_s = get_settings()
Campaign = Annotated[InternalContext, Depends(require_internal(*_s.campaign_clients))]
Transfer = Annotated[InternalContext, Depends(require_internal(*_s.transfer_clients))]
Reader = Annotated[InternalContext, Depends(require_internal(*_s.read_clients))]
Voice = Annotated[InternalContext, Depends(require_internal(*_s.voice_clients))]
idem = IdempotencyStore(idempotency_keys)


def _key(value: str | None) -> str:
    if not value:
        raise Unprocessable("Idempotency-Key header is required")
    return value


async def check_contactable(deps: AppDeps, tenant_id: uuid.UUID, contact_id: uuid.UUID, actor: Any) -> None:
    try:
        verdict = await deps.client("crm-service").get_json(
            f"/internal/v1/contacts/{contact_id}/contactability",
            params={"channel": "phone"},
            tenant_id=tenant_id,
            actor=actor,
        )
    except UpstreamError as exc:
        if exc.is_not_found:
            raise NotContactable("Contact not found", extra={"reasons": ["contact_not_found"]}) from exc
        raise ServiceUnavailable("Contactability check unavailable") from exc
    except UpstreamUnavailable as exc:
        raise ServiceUnavailable("Contactability check unavailable") from exc
    if not verdict.get("allowed"):
        reasons = [str(r)[:32] for r in (verdict.get("reasons") or ["not_contactable"])][:5]
        raise NotContactable("Contact is not contactable by phone", extra={"reasons": reasons})


async def insert_outbound_call(
    s: Any,
    deps: AppDeps,
    *,
    tenant_id: uuid.UUID,
    actor: Any,
    contact_id: uuid.UUID,
    agent_id: uuid.UUID,
    campaign_id: uuid.UUID | None = None,
    campaign_contact_id: uuid.UUID | None = None,
    scheduled_for: datetime | None = None,
    is_test: bool = False,
) -> dict[str, Any]:
    """Insert a `queued` (or `scheduled`) outbound call with its directory entry and state event."""
    now = datetime.now(UTC)
    row = (
        (
            await s.execute(
                insert(calls)
                .values(
                    tenant_id=tenant_id,
                    direction="outbound",
                    state="scheduled" if scheduled_for else "queued",
                    contact_id=contact_id,
                    agent_id=agent_id,
                    campaign_id=campaign_id,
                    campaign_contact_id=campaign_contact_id,
                    scheduled_for=scheduled_for,
                    is_test=is_test,
                    created_by=str(actor)[:160],
                )
                .returning(calls)
            )
        )
        .mappings()
        .one()
    )
    call = dict(row)
    check_at = scheduled_for or now + timedelta(seconds=deps.settings.stale_queued_seconds)
    await register_directory(s, call_id=call["id"], tenant_id=tenant_id, next_check_at=check_at, reason="dispatch")
    await emit(
        s,
        "call.state_changed",
        tenant_id=tenant_id,
        actor=actor,
        payload={
            "call_id": call["id"],
            "from_state": None,
            "to_state": call["state"],
            "direction": "outbound",
            "campaign_id": campaign_id,
        },
    )
    return call


@router.post("/calls", status_code=201, response_model=CreateCallOut)
async def create_call(
    body: CreateCallIn,
    request: Request,
    ictx: Campaign,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    """One call per contact attempt (campaign key `campaign:{id}:contact:{cid}:attempt:{n}`)."""
    deps = AppDeps(request.app)
    tenant_id = ictx.require_tenant()
    now = datetime.now(UTC)
    scheduled = body.scheduled_for is not None and body.scheduled_for > now
    async with deps.db.tenant_session(tenant_id, ictx.actor) as s:
        op = await idem.begin(
            s,
            tenant_id=tenant_id,
            operation="internal.calls.create",
            key=_key(idempotency_key),
            payload=body.model_dump(mode="json"),
        )
        if not op.is_new:
            return JSONResponse(op.response_body, status_code=op.response_status or 201)
        # Concurrent duplicates block on the key row above until this transaction ends, then replay.
        await check_contactable(deps, tenant_id, body.contact_id, ictx.actor)
        call = await insert_outbound_call(
            s,
            deps,
            tenant_id=tenant_id,
            actor=ictx.actor,
            contact_id=body.contact_id,
            agent_id=body.agent_id,
            campaign_id=body.campaign_id,
            campaign_contact_id=body.campaign_contact_id,
            scheduled_for=body.scheduled_for if scheduled else None,
        )
        out = {"call_id": str(call["id"]), "state": call["state"]}
        await idem.succeed(s, op.record_id, status=201, body=out, resource_id=str(call["id"]))
    await after_commit(deps.redis, call)
    if not scheduled:
        await deps.enqueue("calls.dispatch", "calls.dispatch", job_ctx(tenant_id, ictx.actor), call_id=str(call["id"]))
    return JSONResponse(out, status_code=201)


async def cancel_or_hangup(
    deps: AppDeps, tenant_id: uuid.UUID, call_id: uuid.UUID, reason: str, actor: Any
) -> tuple[dict[str, Any], str]:
    """Pending calls are canceled; live calls get a provider hangup (the status callback ends the row)."""
    async with deps.db.tenant_session(tenant_id, actor) as s:
        call = await load_call(s, call_id, lock=True)
        row = None
        action = "not_live"
        if call["state"] in PENDING or (call["state"] == "initiating" and call["attempt_count"] == 0):
            row = await transition(s, call, "canceled", end_reason=reason[:64], actor=actor)
            action = "canceled"
        elif call["state"] in LIVE:
            action = "requested"
    await after_commit(deps.redis, row)
    if action == "requested":
        await deps.enqueue("calls.hangup", "calls.dispatch", job_ctx(tenant_id, actor), call_id=str(call_id))
    return (row or call), action


@router.post("/calls/{call_id}/cancel", response_model=CancelOut)
async def cancel_call(call_id: uuid.UUID, body: ReasonIn, request: Request, ictx: Campaign) -> CancelOut:
    row, _ = await cancel_or_hangup(AppDeps(request.app), ictx.require_tenant(), call_id, body.reason, ictx.actor)
    return CancelOut(call_id=call_id, state=row["state"])


async def _handoff_target(s: Any, call: dict[str, Any]) -> str | None:
    stmt = select(inbound_numbers.c.handoff_e164).where(inbound_numbers.c.status == "active")
    if call["inbound_number_id"] is not None:
        stmt = stmt.where(inbound_numbers.c.id == call["inbound_number_id"])
    elif call["from_e164"]:
        stmt = stmt.where(inbound_numbers.c.e164 == call["from_e164"])
    else:
        return None
    return (await s.execute(stmt)).scalar_one_or_none()


@router.post("/calls/{call_id}/transfer", response_model=TransferOut)
async def transfer_call(
    call_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    ictx: Transfer,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    """Human handoff through a Twilio conference: the caller is moved in and the tenant's agent line is rung."""
    deps = AppDeps(request.app)
    tenant_id = ictx.require_tenant()
    async with deps.db.tenant_session(tenant_id, ictx.actor) as s:
        op = await idem.begin(
            s,
            tenant_id=tenant_id,
            operation=f"internal.calls.transfer:{call_id}",
            key=_key(idempotency_key),
            payload=body.model_dump(mode="json"),
        )
        if not op.is_new:
            return JSONResponse(op.response_body, status_code=op.response_status or 200)
        call = await load_call(s, call_id, lock=True)
        target = await _handoff_target(s, call) if call["state"] in ANSWERED and call["provider_call_id"] else None
        status = "unavailable"
        if target is not None and call["handoff_state"] != "transferring":
            conference_name = f"handoff-{call_id}"
            try:
                creds = await twilio_credentials(deps, tenant_id)
                await deps.telephony.transfer_to_conference(creds, call["provider_call_id"], conference_name)
                await deps.telephony.dial_into_conference(
                    creds,
                    to=target,
                    from_=call["to_e164"] if call["direction"] == "inbound" else call["from_e164"],
                    conference_name=conference_name,
                    idempotency_key=f"handoff:{call_id}",
                )
                status = "transferring"
            except (ProviderRejected, AmbiguousProviderResult, NotSent, ProviderThrottled, ProviderNotConfigured):
                status = "unavailable"
        elif call["handoff_state"] == "transferring":
            status = "transferring"
        await s.execute(
            update(calls)
            .where(calls.c.id == call_id)
            .values(
                handoff_state="transferring" if status == "transferring" else "unavailable",
                version=calls.c.version + 1,
                updated_at=func.now(),
            )
        )
        await audit(
            s, ictx.actor, "call.transfer", "call", call_id, tenant_id=tenant_id, result=status, reason=body.reason
        )
        out = {"status": status}
        await idem.succeed(s, op.record_id, status=200, body=out, resource_id=str(call_id))
    return JSONResponse(out)


@router.get("/calls/{call_id}", response_model=InternalCallOut)
async def get_call(call_id: uuid.UUID, request: Request, ictx: Reader) -> InternalCallOut:
    async with AppDeps(request.app).db.tenant_session(ictx.require_tenant(), ictx.actor) as s:
        call = await load_call(s, call_id)
    return InternalCallOut(
        call_id=call["id"],
        state=call["state"],
        direction=call["direction"],
        contact_id=call["contact_id"],
        agent_id=call["agent_id"],
        campaign_id=call["campaign_id"],
        started_at=call["started_at"],
        ended_at=call["ended_at"],
    )


# ---------------------------------------------------------------- voice-service session hooks
@router.post("/calls/{call_id}/media-started", response_model=AckOut)
async def media_started(call_id: uuid.UUID, request: Request, ictx: Voice) -> AckOut:
    deps = AppDeps(request.app)
    async with deps.db.tenant_session(ictx.require_tenant(), ictx.actor) as s:
        call = await load_call(s, call_id, lock=True)
        row = await transition(s, call, "in_progress", actor=ictx.actor)
    await after_commit(deps.redis, row)
    return AckOut(call_id=call_id, state=(row or call)["state"])


@router.post("/calls/{call_id}/opt-out", response_model=OptOutOut)
async def opt_out(call_id: uuid.UUID, request: Request, ictx: Voice) -> OptOutOut:
    """The caller asked not to be called again (detected deterministically by voice-service, never by a model).

    The call ends as `opted_out`; `call.opted_out` makes crm suppress the contact (voice-service also writes the
    suppression synchronously). Idempotent: a second request changes nothing."""
    deps = AppDeps(request.app)
    tenant_id = ictx.require_tenant()
    async with deps.db.tenant_session(tenant_id, ictx.actor) as s:
        call = await load_call(s, call_id, lock=True)
        if not call["opted_out"]:
            await s.execute(
                update(calls)
                .where(calls.c.id == call_id)
                .values(opted_out=True, version=calls.c.version + 1, updated_at=func.now())
            )
            await emit(
                s,
                "call.opted_out",
                tenant_id=tenant_id,
                actor=ictx.actor,
                payload={"call_id": call_id, "contact_id": call["contact_id"]},
            )
            await audit(s, ictx.actor, "call.opted_out", "call", call_id, tenant_id=tenant_id)
    return OptOutOut(call_id=call_id, opted_out=True)


@router.post("/calls/{call_id}/hangup", response_model=AckOut)
async def voice_hangup(call_id: uuid.UUID, body: ReasonIn, request: Request, ictx: Voice) -> AckOut:
    row, _ = await cancel_or_hangup(AppDeps(request.app), ictx.require_tenant(), call_id, body.reason, ictx.actor)
    return AckOut(call_id=call_id, state=row["state"])
