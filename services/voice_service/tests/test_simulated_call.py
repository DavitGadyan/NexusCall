"""Fully simulated calls, end to end, over real loopback sockets:

    call-service  --REST-->  Twilio simulator  --signed webhooks-->  call-service  --TwiML <Stream>-->
    Twilio simulator's scripted caller  --Media Streams WS-->  voice-service  --WS-->  Deepgram simulator
    voice-service  --SSE-->  stub AI Gateway ;  voice-service  --HTTP-->  ElevenLabs simulator

Both services run their real apps against their own databases (restricted roles, forced RLS). Only the other
platform services (tenant settings, crm, campaign, ai-agent, ai-gateway) are a small local stub.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from nexus_core.celery_app import JobContext
from nexus_core.db import Database
from nexus_core.storage import InMemoryObjectStore
from nexus_core.testing import TEST_AUDIENCE, TEST_ISSUER, StaticAuthzResolver, auth, provision_and_migrate

pytestmark = [pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[3]
FAKE_SECRET = "voice-test-fake-secret"
STREAM_SECRET = "e2e-stream-secret"
TENANT = uuid.uuid4()
AGENT = uuid.uuid4()
DISCLOSURE = "Hello, you are speaking with Acme's AI assistant."
GREETING = "I can help you book an appointment today."
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Platform:
    """Stub for tenant-service, crm, campaign, ai-agent and ai-gateway (records what it receives)."""

    def __init__(self) -> None:
        self.contacts: dict[str, dict[str, Any]] = {}
        self.gateway_requests: list[dict[str, Any]] = []
        self.gateway_headers: list[dict[str, str]] = []
        self.suppressions: list[dict[str, Any]] = []
        self.postprocess: list[dict[str, Any]] = []
        self.app = self._build()

    def _build(self) -> FastAPI:
        app = FastAPI()
        comm = {
            "calls_enabled": True,
            "effective_calls_enabled": True,
            "calling_window": {"start": "00:00", "end": "23:59", "days": ALL_DAYS},
            "max_concurrent_calls": 10,
            "max_daily_calls": 100,
            "allowed_call_countries": ["US"],
            "ai_disclosure_enabled": True,
            "recording_enabled": True,
            "recording_consent_prompt": True,
        }

        @app.get("/internal/v1/tenants/{tenant_id}/settings/{section}")
        async def settings(tenant_id: str, section: str) -> dict[str, Any]:
            if section == "communication":
                return comm
            if section == "retention":
                return {"recordings_days": 90, "transcripts_days": 180}
            return {"timezone": "UTC", "business_hours": None}

        @app.get("/internal/v1/contacts/lookup")
        async def lookup(phone: str) -> dict[str, Any]:
            found = next((c for c, v in self.contacts.items() if v["phone_e164"] == phone), None)
            return {"contact_id": found}

        @app.get("/internal/v1/contacts/{contact_id}/contactability")
        async def contactability(contact_id: str) -> dict[str, Any]:
            return self.contacts[contact_id]

        @app.post("/internal/v1/suppressions", status_code=201)
        async def suppress(request: Request) -> dict[str, Any]:
            body = await request.json()
            self.suppressions.append({**body, "idempotency_key": request.headers.get("idempotency-key")})
            return {"suppression_id": str(uuid.uuid4())}

        @app.get("/internal/v1/campaigns/{campaign_id}/dispatch-state")
        async def dispatch_state(campaign_id: str) -> dict[str, Any]:
            return {"campaign_id": campaign_id, "state": "running", "dispatch_allowed": True}

        @app.get("/internal/v1/agents/{agent_id}/runtime-config")
        async def runtime_config(agent_id: str) -> dict[str, Any]:
            return {
                "agent_id": agent_id,
                "version": 3,
                "kind": "voice",
                "system_prompt": "You are Acme Home Services' scheduling assistant.",
                "model": None,
                "voice_id": "acme-voice",
                "tool_ids": ["booking.find_slots", "booking.create_tentative", "booking.confirm"],
                "greeting": GREETING,
                "disclosure_text": DISCLOSURE,
                "escalation_rules": [],
                "max_call_seconds": 120,
            }

        @app.post("/internal/v1/calls/postprocess")
        async def postprocess(request: Request) -> dict[str, Any]:
            self.postprocess.append(await request.json())
            return {"summary": "Booked", "outcome": "booked", "qualified": True}

        @app.post("/internal/v1/ai/responses/stream")
        async def stream(request: Request) -> StreamingResponse:
            body = await request.json()
            self.gateway_requests.append(body)
            self.gateway_headers.append(dict(request.headers))
            last = body["messages"][-1]["content"][-1]["text"].lower()
            if "book" in last:
                reply = "I can book you for Tuesday at ten in the morning. Does that work for you?"
            elif "goodbye" in last:
                reply = "Thank you for calling Acme. Goodbye."
            elif "ignore" in last:
                reply = "I can't share that, but I am happy to help you book an appointment."
            else:
                reply = "Hello! How can I help you today?"

            async def events():
                for word in reply.split(" "):
                    yield f"data: {json.dumps({'type': 'text_delta', 'text': word + ' '})}\n\n".encode()
                yield f"data: {json.dumps({'type': 'done', 'stop_reason': 'end_turn', 'usage': {}})}\n\n".encode()

            return StreamingResponse(events(), media_type="text/event-stream")

        return app


def _sql(url: str, query: str, params: dict[str, Any] | None = None, tenant: uuid.UUID | None = TENANT) -> list[Any]:
    async def go() -> list[Any]:
        db = Database(url, pool_size=1, max_overflow=0)
        try:
            ctx = db.tenant_session(tenant) if tenant else db.global_session()
            async with ctx as s:
                return [dict(r) for r in (await s.execute(text(query), params or {})).mappings().all()]
        finally:
            await db.dispose()

    return asyncio.run(go())


def _wait(predicate: Any, what: str, limit: float = 45.0) -> Any:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def _inline_jobs(app: Any, table: dict[str, Any], deps_factory: Any, arg: str) -> Any:
    """`enqueue` replacement: run the task coroutine right away in the server's own event loop."""

    async def enqueue(name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        fn = table.get(name)
        if fn is None:
            return
        key = arg if arg in ids else next(iter(ids))

        async def run() -> None:
            try:
                await fn(deps_factory(app), ctx, uuid.UUID(ids[key]))
            except Exception:
                import logging

                logging.getLogger("e2e").exception("inline_job_failed", extra={"job": name})

        asyncio.get_running_loop().create_task(run())

    return enqueue


@pytest.fixture
def stack(pg, sdb, tokens, server, fake_providers_app, monkeypatch):
    from call_service.deps import AppDeps as CallDeps
    from call_service.jobs import dispatch_call, hangup_call, process_provider_event, reconcile_call
    from call_service.main import create_app as create_call_app
    from call_service.settings import Settings as CallSettings
    from voice_service.deps import AppDeps as VoiceDeps
    from voice_service.jobs import postprocess_session
    from voice_service.main import create_app as create_voice_app
    from voice_service.settings import Settings as VoiceSettings

    call_db = provision_and_migrate(pg, "call", ROOT / "services" / "call_service")
    monkeypatch.setenv("FAKE_TWILIO_PLAYBACK_FACTOR", "0.02")
    platform = Platform()
    common = {
        "env": "test",
        "oidc_issuer": TEST_ISSUER,
        "oidc_audience": TEST_AUDIENCE,
        "redis_url": "redis://127.0.0.1:1/0",
        "outbox_relay_enabled": False,
        "event_consumers_enabled": False,
        "periodic_jobs_enabled": False,
        "provider_mode": "fake",
        "fake_provider_secret": FAKE_SECRET,
        "stream_token_secret": STREAM_SECRET,
    }
    with server(fake_providers_app) as fake_url, server(platform.app) as platform_url:
        for svc in ("tenant-service", "crm-service", "campaign-service", "ai-agent-service", "ai-gateway"):
            monkeypatch.setenv(f"SVC_{svc.replace('-', '_').upper()}_URL", platform_url)
        store = InMemoryObjectStore()
        voice_app = create_voice_app(
            VoiceSettings(
                **common,
                database_url=sdb.app_url,
                fake_providers_url=fake_url,
                no_speech_timeout_seconds=6.0,
                endpointing_ms=300,
            ),
            authz_resolver=StaticAuthzResolver(),
            store=store,
            enqueue=None,
        )
        voice_app.state.enqueue_override = _inline_jobs(
            voice_app, {"voice.postprocess": postprocess_session}, VoiceDeps, "session_id"
        )

        async def voice_token() -> str:
            return tokens.service("svc-voice-service")

        voice_app.state.token_verifier = tokens.verifier()
        voice_app.state.service_token = voice_token
        voice_app.state.redis = None
        with server(voice_app) as voice_url:
            voice_app.state.enqueue = voice_app.state.enqueue_override
            call_settings = CallSettings(
                **common,
                database_url=call_db.app_url,
                fake_providers_url=fake_url,
                media_stream_url="ws" + voice_url[4:] + "/media/twilio",
                webhook_base_url="http://placeholder",
            )
            call_app = create_call_app(call_settings, authz_resolver=StaticAuthzResolver())
            jobs = {
                "calls.dispatch": dispatch_call,
                "calls.reconcile": reconcile_call,
                "calls.hangup": hangup_call,
                "calls.provider_callback": process_provider_event,
            }

            async def call_token() -> str:
                return tokens.service("svc-call-service")

            call_app.state.token_verifier = tokens.verifier()
            call_app.state.service_token = call_token
            call_app.state.redis = None
            with server(call_app) as call_url:
                # Twilio signs the URLs it is given, so the public webhook base must be the real address.
                call_app.state.settings = call_settings.model_copy(update={"webhook_base_url": call_url})
                call_app.state.enqueue = _inline_jobs(call_app, jobs, CallDeps, "call_id")
                monkeypatch.setenv("SVC_CALL_SERVICE_URL", call_url)
                yield {
                    "platform": platform,
                    "fake": fake_url,
                    "call": call_url,
                    "voice": voice_url,
                    "call_db": call_db.app_url,
                    "voice_db": sdb.app_url,
                    "store": store,
                    "call_app": call_app,
                    "voice_app": voice_app,
                    "tokens": tokens,
                }


def _add_contact(platform: Platform, phone: str) -> uuid.UUID:
    cid = uuid.uuid4()
    platform.contacts[str(cid)] = {
        "contact_id": str(cid),
        "allowed": True,
        "reasons": [],
        "phone_e164": phone,
        "timezone": "America/New_York",
        "country": "US",
    }
    return cid


def _place_call(stack: dict[str, Any], contact: uuid.UUID) -> str:
    tokens = stack["tokens"]
    r = httpx.post(
        f"{stack['call']}/internal/v1/calls",
        headers=auth(tokens.service("svc-campaign-service"), TENANT, **{"Idempotency-Key": uuid.uuid4().hex}),
        json={"contact_id": str(contact), "agent_id": str(AGENT), "campaign_id": str(uuid.uuid4())},
        timeout=10,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["call_id"])


def _call_row(stack: dict[str, Any], call_id: str) -> dict[str, Any] | None:
    rows = _sql(stack["call_db"], "SELECT * FROM calls WHERE id = :i", {"i": call_id})
    return rows[0] if rows else None


def _session(stack: dict[str, Any], call_id: str) -> dict[str, Any] | None:
    rows = _sql(stack["voice_db"], "SELECT * FROM voice_sessions WHERE call_id = :c", {"c": call_id})
    return rows[0] if rows else None


def _sim(stack: dict[str, Any], provider_call_id: str) -> dict[str, Any]:
    return httpx.get(f"{stack['fake']}/twilio/_control/calls/{provider_call_id}", timeout=5).json()


def _final_event(stack: dict[str, Any], call_id: str) -> dict[str, Any]:
    rows = _sql(
        stack["call_db"],
        "SELECT envelope FROM outbox WHERE event_type = 'call.completed' AND envelope->'payload'->>'call_id' = :c",
        {"c": call_id},
        tenant=None,
    )
    assert len(rows) == 1, rows
    return rows[0]["envelope"]["payload"]


def test_fully_simulated_outbound_booking_call(stack):  # noqa: PLR0915 - one scenario walk-through
    platform: Platform = stack["platform"]
    manager = uuid.uuid4()
    stack["call_app"].state.authz_resolver.grant(manager, TENANT, {"calls:manage", "calls:read"})
    token = stack["tokens"].user(manager, "manager@example.test")
    created = httpx.post(
        f"{stack['call']}/api/v1/inbound-numbers",
        headers=auth(token, TENANT),
        json={"e164": "+12125550100", "agent_id": str(AGENT), "handoff_e164": "+12125550198"},
        timeout=10,
    )
    assert created.status_code == 201, created.text

    # ---- 1. outbound booking call ------------------------------------------------------------
    contact = _add_contact(platform, "+12125550123")
    call_id = _place_call(stack, contact)
    _wait(lambda: (_call_row(stack, call_id) or {}).get("state") == "completed", "booking call to complete")
    session = _wait(lambda: (s := _session(stack, call_id)) and s["postprocess_status"] == "done" and s, "postprocess")
    call = _call_row(stack, call_id)
    assert call is not None and call["provider_call_id"].startswith("CA") and call["answered_at"] is not None
    assert _final_event(stack, call_id)["final_state"] == "completed"
    sim = _sim(stack, call["provider_call_id"])
    said = " ".join(sim["agent_said"])
    assert DISCLOSURE in said and "may be recorded" in said and GREETING in said  # disclosure + consent first
    assert "Tuesday at ten" in said
    assert sim["clears"] == 0
    segments = _sql(
        stack["voice_db"],
        "SELECT role, text FROM transcript_segments WHERE session_id = :s ORDER BY seq",
        {"s": str(session["id"])},
    )
    caller_lines = [s["text"] for s in segments if s["role"] == "caller"]
    assert any("book an appointment" in line for line in caller_lines)
    assert session["disclosure_played"] and session["consent_prompt_played"] and session["status"] == "ended"
    # The AI Gateway got the caller's words only as untrusted transcript, plus a narrow tool set.
    turns = [r for r in platform.gateway_requests if r["context"]["call_id"] == call_id]
    assert turns and all(r["purpose"] == "voice_turn" for r in turns)
    for req in turns:
        assert req["tool_ids"] == ["booking.create_tentative", "booking.find_slots"]  # booking.confirm dropped
        for message in req["messages"]:
            for block in message["content"]:
                if message["role"] == "user":
                    assert block["source"] == "transcript" and block["trust"] == "untrusted"
                    assert block["text"] not in req["system"]
    assert all(h["x-tenant-id"] == str(TENANT) for h in platform.gateway_headers)
    # Transcript and dual-channel recording were stored under the tenant's prefix; post-processing ran.
    keys = {k for _, k in stack["store"].objects}
    assert f"{TENANT}/calls/{call_id}/transcript.json" in keys and f"{TENANT}/calls/{call_id}/recording.wav" in keys
    post = next(p for p in platform.postprocess if p["call_id"] == call_id)
    assert post["agent_id"] == str(AGENT) and any(t["role"] == "caller" for t in post["transcript"])

    # ---- 2. outbound call where the caller opts out ------------------------------------------
    opt_contact = _add_contact(platform, "+12125550124")
    httpx.post(f"{stack['fake']}/twilio/_control/scripts", json={"to": "+12125550124", "script": "opt_out"}, timeout=5)
    opt_call = _place_call(stack, opt_contact)
    _wait(lambda: (_call_row(stack, opt_call) or {}).get("state") == "opted_out", "opt-out call to end")
    assert _final_event(stack, opt_call)["final_state"] == "opted_out"
    opt_session = _wait(lambda: _session(stack, opt_call), "opt-out session")
    assert opt_session["opted_out"] is True and opt_session["end_reason"] == "opted_out"
    assert any(s["contact_id"] == str(opt_contact) and s["reason"] == "opt_out" for s in platform.suppressions)
    opted = _sql(
        stack["call_db"],
        "SELECT count(*) AS n FROM outbox WHERE event_type = 'call.opted_out' AND envelope->'payload'->>'call_id' = :c",
        {"c": opt_call},
        tenant=None,
    )
    assert opted[0]["n"] == 1
    # The opt-out was handled in code: the model never saw the opt-out turn.
    for req in platform.gateway_requests:
        if req["context"]["call_id"] == opt_call:
            assert all("stop calling" not in b["text"].lower() for m in req["messages"] for b in m["content"])
    assert "will not call you again" in " ".join(
        _sim(stack, _call_row(stack, opt_call)["provider_call_id"])["agent_said"]
    )

    # ---- 3. inbound call with a prompt-injection attempt -------------------------------------
    sid = httpx.post(
        f"{stack['fake']}/twilio/_simulate/inbound",
        json={
            "to": "+12125550100",
            "from": "+12125550123",
            "script": "injection",
            "voice_url": f"{stack['call']}/webhooks/twilio/voice",
            "status_url": f"{stack['call']}/webhooks/twilio/status",
        },
        timeout=5,
    ).json()["sid"]
    inbound = _wait(
        lambda: (rows := _sql(stack["call_db"], "SELECT * FROM calls WHERE provider_call_id = :s", {"s": sid}))
        and rows[0]["state"] == "completed"
        and rows[0],
        "inbound call to complete",
    )
    assert inbound["direction"] == "inbound" and str(inbound["contact_id"]) == str(contact)
    inbound_turns = [r for r in platform.gateway_requests if r["context"]["call_id"] == str(inbound["id"])]
    injected = [
        b
        for r in inbound_turns
        for m in r["messages"]
        for b in m["content"]
        if "ignore all previous instructions" in b["text"].lower()
    ]
    assert injected and all(b["trust"] == "untrusted" and b["source"] == "transcript" for b in injected)
    assert all("ignore all previous" not in r["system"].lower() for r in inbound_turns)
