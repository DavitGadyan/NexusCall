"""Twilio webhooks (provider-signed, no user auth). BUILD_CONVENTIONS §3 Security / webhooks:

* the tenant comes from OUR rows: `call_directory` (the call id we put in the callback URL, or the CallSid we
  stored) or `number_directory` (the dialed number). Nothing in the payload chooses the tenant;
* the signature is verified with THAT tenant's Twilio auth token (CredentialResolver), over the exact public URL;
* status callbacks are deduplicated on (provider, event id) and processed on `calls.provider_callbacks`;
* the answer/voice webhooks must return TwiML synchronously, so they do only small, bounded work.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from call_service.deps import AppDeps, twilio_credentials
from call_service.domain import (
    SERVICE_ACTOR,
    after_commit,
    emit,
    find_by_provider_id,
    job_ctx,
    load_call,
    register_directory,
    set_provider_call_id,
    transition,
)
from call_service.gates import in_window, is_e164, zone
from call_service.state_machine import TERMINAL
from call_service.stream_token import mint_stream_token
from call_service.tables import call_directory, calls, inbound_numbers, number_directory, provider_events
from call_service.twiml import (
    REJECT_AFTER_HOURS,
    REJECT_UNAVAILABLE,
    REJECT_UNKNOWN,
    connect_stream,
    hangup,
    say_and_hangup,
)
from nexus_core.clients import UpstreamError, UpstreamUnavailable, tenant_settings_section
from nexus_core.credentials import ProviderNotConfigured
from nexus_core.errors import Conflict, Forbidden, NotFound, Unprocessable
from nexus_core.webhooks import WebhookVerificationError, verify_twilio

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"], include_in_schema=False)
log = logging.getLogger(__name__)

_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_MAX_PARAMS = 100
# Only these provider fields are kept (no caller numbers, names or locations).
_STATUS_FIELDS = ("CallStatus", "CallDuration", "Duration", "SequenceNumber", "AnsweredBy", "Price")


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    try:
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=_MAX_PARAMS)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Unprocessable("Malformed webhook body") from exc
    return dict(pairs)


def _public_url(request: Request) -> str:
    """The URL Twilio signed: our configured public base + path + the query string as received."""
    base = request.app.state.settings.webhook_base_url.rstrip("/")
    query = request.url.query
    return f"{base}{request.url.path}" + (f"?{query}" if query else "")


async def _verify(deps: AppDeps, tenant_id: uuid.UUID, request: Request, params: dict[str, str]) -> None:
    try:
        creds = await twilio_credentials(deps, tenant_id)
        verify_twilio(creds.auth_token, _public_url(request), params, request.headers.get("x-twilio-signature"))
    except (WebhookVerificationError, ProviderNotConfigured) as exc:
        log.warning("twilio_webhook_rejected", extra={"reason": type(exc).__name__})
        raise Forbidden("Invalid webhook signature") from exc


async def _tenant_for_call(
    deps: AppDeps, call_id: uuid.UUID | None, call_sid: str | None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with deps.db.global_session(SERVICE_ACTOR) as s:
        stmt = select(call_directory.c.tenant_id, call_directory.c.call_id)
        if call_id is not None:
            stmt = stmt.where(call_directory.c.call_id == call_id)
        elif call_sid is not None:
            stmt = stmt.where(call_directory.c.provider_call_id == call_sid)
        else:
            raise NotFound("Unknown call")
        row = (await s.execute(stmt)).first()
    if row is None:
        raise NotFound("Unknown call")
    return row.tenant_id, row.call_id


def _call_id_param(request: Request) -> uuid.UUID | None:
    raw = request.query_params.get("c")
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise NotFound("Unknown call") from exc


def _event_data(params: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {"call_sid": params.get("CallSid")}
    duration = params.get("CallDuration") or params.get("Duration")
    if duration and duration.isdigit():
        data["duration_seconds"] = int(duration)
    price = params.get("Price")
    if price:
        with contextlib.suppress(ValueError):
            data["price_micros"] = round(abs(float(price)) * 1_000_000)
    if params.get("AnsweredBy"):
        data["answered_by"] = params["AnsweredBy"][:32]
    return data


@router.post("/status", status_code=204)
async def status_callback(request: Request) -> Response:
    deps = AppDeps(request.app)
    params = await _form(request)
    call_sid = params.get("CallSid")
    if not call_sid or not _CALL_SID.match(call_sid):
        raise Unprocessable("Missing CallSid")
    tenant_id, call_id = await _tenant_for_call(deps, _call_id_param(request), call_sid)
    await _verify(deps, tenant_id, request, params)
    status = (params.get("CallStatus") or "")[:16]
    seq_raw = params.get("SequenceNumber")
    sequence = int(seq_raw) if seq_raw and seq_raw.isdigit() else None
    event_key = f"status:{call_sid}:{status}:{seq_raw or ''}"
    event_id = hashlib.sha256(event_key.encode()).hexdigest()
    async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
        call = await load_call(s, call_id)  # our row, in the tenant we resolved; 404 otherwise
        if call["provider_call_id"] not in (None, call_sid):
            raise Forbidden("Call identifier mismatch")
        new_id = (
            await s.execute(
                insert(provider_events)
                .values(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    provider="twilio",
                    event_id=event_id,
                    event_type="status",
                    provider_status=status,
                    sequence=sequence,
                    data=_event_data(params),
                )
                .on_conflict_do_nothing(constraint="uq_provider_events_event")
                .returning(provider_events.c.id)
            )
        ).scalar_one_or_none()
        if new_id is not None:
            soon = datetime.now(UTC) + timedelta(seconds=60)
            await s.execute(
                update(call_directory)
                .where(call_directory.c.call_id == call_id)
                .values(
                    next_check_at=func.least(func.coalesce(call_directory.c.next_check_at, soon), soon),
                    check_reason=func.coalesce(call_directory.c.check_reason, "callbacks"),
                )
            )
    if new_id is None:
        return Response(status_code=204)  # duplicate delivery: already recorded
    try:
        await deps.enqueue(
            "calls.provider_callback",
            "calls.provider_callbacks",
            job_ctx(tenant_id, SERVICE_ACTOR),
            event_id=str(new_id),
        )
    except Exception:  # noqa: BLE001 - the sweeper re-enqueues received events; still acknowledge Twilio
        log.warning("provider_callback_enqueue_failed", extra={"call_id": str(call_id)})
    return Response(status_code=204)


def _token_for(deps: AppDeps, call: dict[str, Any], call_sid: str) -> str:
    settings = deps.settings
    return mint_stream_token(
        settings.stream_secret(),
        tenant_id=call["tenant_id"],
        call_id=call["id"],
        call_sid=call_sid,
        direction=call["direction"],
        agent_id=call["agent_id"],
        contact_id=call["contact_id"],
        ttl_seconds=settings.stream_token_ttl_seconds,
    )


@router.post("/answer")
async def answer(request: Request) -> Response:
    """Outbound call answered: connect the media stream to voice-service."""
    deps = AppDeps(request.app)
    params = await _form(request)
    call_sid = params.get("CallSid")
    if not call_sid or not _CALL_SID.match(call_sid):
        raise Unprocessable("Missing CallSid")
    call_id = _call_id_param(request)
    if call_id is None:
        raise NotFound("Unknown call")
    tenant_id, _ = await _tenant_for_call(deps, call_id, None)
    await _verify(deps, tenant_id, request, params)
    answered_by = params.get("AnsweredBy", "")
    row = None
    async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
        call = await load_call(s, call_id, lock=True)
        try:
            call = await set_provider_call_id(s, call, call_sid)
        except Conflict as exc:
            raise Forbidden("Call identifier mismatch") from exc
        if call["state"] in TERMINAL:
            return hangup()
        if answered_by.startswith("machine") or answered_by == "fax":
            await s.execute(update(calls).where(calls.c.id == call_id).values(end_reason="answering_machine"))
            return hangup()
        row = await transition(s, call, "connected")
        current = row or call
    await after_commit(deps.redis, row)
    return connect_stream(deps.settings.media_stream_url, _token_for(deps, current, call_sid))


async def _business_hours_open(deps: AppDeps, tenant_id: uuid.UUID) -> bool:
    profile = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "profile")
    window = profile.get("business_hours")
    return not window or in_window(window, datetime.now(UTC), zone(profile.get("timezone")))


async def _lookup_contact(deps: AppDeps, tenant_id: uuid.UUID, phone: str | None) -> uuid.UUID | None:
    if not phone or not is_e164(phone):
        return None
    try:
        found = await deps.client("crm-service").request(
            "GET",
            "/internal/v1/contacts/lookup",
            params={"phone": phone},
            tenant_id=tenant_id,
            actor=SERVICE_ACTOR,
            timeout_seconds=2.0,
        )
        value = found.json().get("contact_id")
        return uuid.UUID(str(value)) if value else None
    except (UpstreamError, UpstreamUnavailable, ValueError):
        return None  # the call proceeds without a contact match


@router.post("/voice")
async def inbound_voice(request: Request) -> Response:
    """Inbound call. The DIALED number (trusted `number_directory`) decides the tenant and agent."""
    deps = AppDeps(request.app)
    params = await _form(request)
    call_sid, to = params.get("CallSid"), params.get("To") or params.get("Called")
    if not call_sid or not _CALL_SID.match(call_sid) or not to or not is_e164(to):
        return say_and_hangup(REJECT_UNKNOWN)
    async with deps.db.global_session(SERVICE_ACTOR) as s:
        entry = (await s.execute(select(number_directory).where(number_directory.c.e164 == to))).mappings().first()
    if entry is None or entry["status"] != "active":
        # Unknown numbers cannot be verified (no tenant token); nothing is stored or revealed.
        return say_and_hangup(REJECT_UNKNOWN)
    tenant_id = entry["tenant_id"]
    await _verify(deps, tenant_id, request, params)
    try:
        comm = await tenant_settings_section(deps.client("tenant-service"), deps.redis, tenant_id, "communication")
    except (UpstreamError, UpstreamUnavailable):
        return say_and_hangup(REJECT_UNAVAILABLE)  # fail closed
    if not comm.get("effective_calls_enabled", False):
        return say_and_hangup(REJECT_UNAVAILABLE)
    async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
        number = (
            (await s.execute(select(inbound_numbers).where(inbound_numbers.c.id == entry["inbound_number_id"])))
            .mappings()
            .first()
        )
        existing = await find_by_provider_id(s, call_sid)
    if number is None or number["status"] != "active":
        return say_and_hangup(REJECT_UNKNOWN)
    if existing is not None:  # Twilio retried the webhook: same call, fresh token
        if existing["state"] in TERMINAL:
            return hangup()
        return connect_stream(deps.settings.media_stream_url, _token_for(deps, existing, call_sid))
    if number["business_hours_only"]:
        try:
            if not await _business_hours_open(deps, tenant_id):
                return say_and_hangup(REJECT_AFTER_HOURS)
        except (UpstreamError, UpstreamUnavailable):
            return say_and_hangup(REJECT_UNAVAILABLE)
    caller = params.get("From")
    contact_id = await _lookup_contact(deps, tenant_id, caller)
    async with deps.db.tenant_session(tenant_id, SERVICE_ACTOR) as s:
        inserted = (
            (
                await s.execute(
                    insert(calls)
                    .values(
                        tenant_id=tenant_id,
                        direction="inbound",
                        state="connected",
                        contact_id=contact_id,
                        agent_id=number["agent_id"],
                        inbound_number_id=number["id"],
                        from_e164=caller if caller and is_e164(caller) else None,
                        to_e164=to,
                        provider_call_id=call_sid,
                        answered_at=func.now(),
                        initiated_at=func.now(),
                    )
                    .on_conflict_do_nothing(constraint="uq_calls_provider_call_id")
                    .returning(calls)
                )
            )
            .mappings()
            .first()
        )
        if inserted is None:  # concurrent duplicate webhook created it first
            existing = await find_by_provider_id(s, call_sid)
            if existing is None:
                return say_and_hangup(REJECT_UNAVAILABLE)
            return connect_stream(deps.settings.media_stream_url, _token_for(deps, existing, call_sid))
        call = dict(inserted)
        await register_directory(
            s,
            call_id=call["id"],
            tenant_id=tenant_id,
            provider_call_id=call_sid,
            next_check_at=datetime.now(UTC) + timedelta(seconds=deps.settings.live_call_max_seconds),
            reason="live",
        )
        await emit(
            s,
            "call.state_changed",
            tenant_id=tenant_id,
            actor=SERVICE_ACTOR,
            payload={
                "call_id": call["id"],
                "from_state": None,
                "to_state": "connected",
                "direction": "inbound",
                "campaign_id": None,
            },
        )
    await after_commit(deps.redis, call)
    return connect_stream(deps.settings.media_stream_url, _token_for(deps, call, call_sid))
