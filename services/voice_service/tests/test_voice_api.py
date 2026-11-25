"""voice-service against a real database: stream authentication, transcript/recording permissions and tenant
isolation, post-processing and retention."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from starlette.websockets import WebSocketDisconnect

from call_service.stream_token import mint_stream_token
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.clients import ServiceClient
from nexus_core.credentials import CredentialResolver
from nexus_core.db import Database
from nexus_core.storage import InMemoryObjectStore, tenant_key
from nexus_core.testing import TEST_AUDIENCE, TEST_ISSUER, StaticAuthzResolver, auth
from voice_service.jobs import postprocess_session, retention_purge
from voice_service.settings import Settings
from voice_service.tables import recordings, session_directory, transcript_segments, voice_sessions

pytestmark = [pytest.mark.integration]
STREAM_SECRET = "voice-api-stream-secret"
SID = "CA" + "d" * 32


def _settings(sdb: Any) -> Settings:
    return Settings(
        env="test",
        database_url=sdb.app_url,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
        redis_url="redis://127.0.0.1:1/0",
        outbox_relay_enabled=False,
        event_consumers_enabled=False,
        periodic_jobs_enabled=False,
        provider_mode="fake",
        fake_provider_secret="voice-test-fake-secret",
        fake_providers_url="http://fake-providers:8000",
        stream_token_secret=STREAM_SECRET,
        start_timeout_seconds=2.0,
    )


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key: str) -> None:
        return None

    async def publish(self, *_: Any) -> int:
        return 0


class VoiceHarness:
    def __init__(self, client: TestClient, app: Any, tokens: Any, sdb: Any, store: InMemoryObjectStore, mock: Any):
        self.client, self.app, self.tokens, self.sdb, self.store, self.mock = client, app, tokens, sdb, store, mock
        self.authz: StaticAuthzResolver = app.state.authz_resolver

    def user(self, tenant: uuid.UUID, perms: set[str]) -> str:
        uid = uuid.uuid4()
        self.authz.grant(uid, tenant, perms)
        return self.tokens.user(uid, f"v-{uid.hex[:6]}@example.test")

    def db_run(self, fn: Any) -> Any:
        async def go() -> Any:
            db = Database(self.sdb.app_url, pool_size=1, max_overflow=0)
            try:
                return await fn(db)
            finally:
                await db.dispose()

        return asyncio.run(go())

    def seed_session(
        self, tenant: uuid.UUID, *, ended_days_ago: int = 0, recording: bool = True
    ) -> tuple[uuid.UUID, uuid.UUID]:
        call_id, session_id = uuid.uuid4(), uuid.uuid4()
        ended = datetime.now(UTC) - timedelta(days=ended_days_ago)
        key = tenant_key(tenant, "calls", str(call_id), "transcript.json")
        rec_key = tenant_key(tenant, "calls", str(call_id), "recording.wav")

        async def go(db: Database) -> None:
            async with db.tenant_session(tenant) as s:
                await s.execute(
                    insert(voice_sessions).values(
                        id=session_id,
                        tenant_id=tenant,
                        call_id=call_id,
                        agent_id=uuid.uuid4(),
                        direction="outbound",
                        status="ended",
                        end_reason="caller_hangup",
                        transcript_key=key,
                        postprocess_status="queued",
                        started_at=ended - timedelta(minutes=3),
                        ended_at=ended,
                        recording_enabled=recording,
                    )
                )
                for seq, (role, said) in enumerate(
                    [("agent", "Hi, this is Acme."), ("caller", "Book me for Tuesday."), ("agent", "Done.")], start=1
                ):
                    await s.execute(
                        insert(transcript_segments).values(
                            tenant_id=tenant,
                            session_id=session_id,
                            seq=seq,
                            role=role,
                            text=said,
                            started_ms=seq * 1000,
                        )
                    )
                if recording:
                    await s.execute(
                        insert(recordings).values(
                            tenant_id=tenant,
                            session_id=session_id,
                            call_id=call_id,
                            bucket="recordings",
                            object_key=rec_key,
                            content_type="audio/wav",
                            duration_seconds=180,
                            size_bytes=4,
                            created_at=ended,
                        )
                    )
                await s.execute(
                    insert(session_directory).values(
                        session_id=session_id, tenant_id=tenant, ended_at=ended, postprocess_due_at=ended
                    )
                )

        self.db_run(go)
        asyncio.run(self.store.put("transcripts", key, b"{}"))
        if recording:
            asyncio.run(self.store.put("recordings", rec_key, b"RIFF"))
        return call_id, session_id

    def sql(self, query: str, params: dict[str, Any] | None = None, *, tenant: uuid.UUID | None = None) -> list[Any]:
        async def go(db: Database) -> list[Any]:
            ctx = db.tenant_session(tenant) if tenant else db.global_session()
            async with ctx as s:
                return list((await s.execute(text(query), params or {})).mappings().all())

        return self.db_run(go)

    def deps(self) -> Any:
        return _Deps(self.app.state.settings, self.store, self.tokens)


class _Deps:
    def __init__(self, settings: Settings, store: InMemoryObjectStore, tokens: Any) -> None:
        self.settings, self.store, self._tokens = settings, store, tokens
        self.db = Database(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
        self.redis = None
        self.credentials = CredentialResolver(settings, None, None)
        self.enqueued: list[Any] = []

    def client(self, service: str) -> ServiceClient:
        async def token() -> str:
            return self._tokens.service("svc-voice-service")

        return ServiceClient(service, self.settings.service_url(service), token)

    async def enqueue(self, name: str, queue: str, ctx: JobContext, **ids: str) -> None:
        self.enqueued.append((name, ids))


@pytest.fixture
def vh(sdb, tokens):
    from voice_service.main import create_app

    store = InMemoryObjectStore()
    app = create_app(_settings(sdb), authz_resolver=StaticAuthzResolver(), store=store, enqueue=None)
    app.state.token_verifier = tokens.verifier()

    async def svc_token() -> str:
        return tokens.service("svc-voice-service")

    with respx.mock(assert_all_called=False) as mock, TestClient(app) as client:
        app.state.service_token = svc_token
        app.state.redis = FakeRedis()
        yield VoiceHarness(client, app, tokens, sdb, store, mock)


# ----------------------------------------------------------------------------------------- stream auth
def _start(token: str | None, sid: str = SID) -> str:
    params = {"token": token} if token is not None else {}
    return json.dumps(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": "MZ" + "0" * 32,
            "start": {"callSid": sid, "streamSid": "MZ" + "0" * 32, "customParameters": params},
        }
    )


def _token(tenant: uuid.UUID, call_id: uuid.UUID, *, secret: str = STREAM_SECRET, ttl: int = 120) -> str:
    return mint_stream_token(
        secret.encode(),
        tenant_id=tenant,
        call_id=call_id,
        call_sid=SID,
        direction="outbound",
        agent_id=uuid.uuid4(),
        contact_id=None,
        ttl_seconds=ttl,
    )


def _close_code(vh: VoiceHarness, *messages: str) -> int:
    with vh.client.websocket_connect("/media/twilio") as ws:
        for m in messages:
            ws.send_text(m)
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                ws.receive_text()
    return exc.value.code


@pytest.mark.security
def test_stream_without_valid_token_is_closed_before_any_work(vh):
    tenant, call_id = uuid.uuid4(), uuid.uuid4()
    route = vh.mock.get(url__regex=r"^http://call-service:8000/internal/v1/calls/").mock(
        return_value=httpx.Response(200, json={"call_id": str(call_id), "state": "completed", "direction": "outbound"})
    )
    connected = json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    media = json.dumps({"event": "media", "media": {"payload": "AAAA"}})
    assert _close_code(vh, connected, media, _start(None)) == 1008
    assert _close_code(vh, _start("garbage")) == 1008
    assert _close_code(vh, _start(_token(tenant, call_id, secret="someone-elses-secret"))) == 1008
    assert _close_code(vh, _start(_token(tenant, call_id, ttl=-5))) == 1008
    assert _close_code(vh, _start(_token(tenant, call_id), sid="CA" + "e" * 32)) == 1008  # other call's stream
    assert route.call_count == 0  # nothing was looked up for unauthenticated streams
    assert vh.sql("SELECT id FROM voice_sessions", tenant=tenant) == []
    # A valid token passes authentication (then the call is not live, so the session is refused) ...
    token = _token(tenant, call_id)
    assert _close_code(vh, _start(token)) == 1008
    assert route.call_count == 1
    # ... and the same token can never be used again.
    assert _close_code(vh, _start(token)) == 1008
    assert route.call_count == 1


def test_stream_start_timeout_closes(vh):
    with vh.client.websocket_connect("/media/twilio") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1008


# ----------------------------------------------------------------------------------------- transcript & recording
@pytest.mark.security
def test_transcript_and_recording_permissions_audit_and_isolation(vh):
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    call_id, _ = vh.seed_session(tenant_a)
    auditor = vh.user(tenant_a, {"calls:recordings:read"})
    reader = vh.user(tenant_a, {"calls:read"})
    other = vh.user(tenant_b, {"calls:recordings:read", "calls:read"})
    t = vh.client.get(f"/api/v1/voice/calls/{call_id}/transcript", headers=auth(auditor, tenant_a))
    assert t.status_code == 200
    assert [s["role"] for s in t.json()["segments"]] == ["agent", "caller", "agent"]
    r = vh.client.get(f"/api/v1/voice/calls/{call_id}/recording", headers=auth(auditor, tenant_a))
    assert (
        r.status_code == 200
        and r.json()["url"].startswith("memory://recordings/")
        and r.json()["expires_in_seconds"] <= 900
    )
    assert str(tenant_a) in r.json()["url"]
    for path in ("transcript", "recording"):
        assert vh.client.get(f"/api/v1/voice/calls/{call_id}/{path}", headers=auth(reader, tenant_a)).status_code == 403
        assert vh.client.get(f"/api/v1/voice/calls/{call_id}/{path}", headers=auth(other, tenant_b)).status_code == 404
        assert vh.client.get(f"/api/v1/voice/calls/{call_id}/{path}", headers=auth(other, tenant_a)).status_code == 403
        assert vh.client.get(f"/api/v1/voice/calls/{call_id}/{path}").status_code == 401
    meta = vh.client.get(f"/api/v1/voice/calls/{call_id}", headers=auth(reader, tenant_a))
    assert meta.status_code == 200 and meta.json()["recording_available"] is True and "segments" not in meta.json()
    assert vh.client.get(f"/api/v1/voice/calls/{call_id}", headers=auth(other, tenant_b)).status_code == 404
    actions = [
        row["envelope"]["payload"]["action"]
        for row in vh.sql("SELECT envelope FROM outbox WHERE event_type = 'audit.event'")
        if row["envelope"]["tenant_id"] == str(tenant_a)
    ]
    assert actions.count("call.transcript.accessed") == 1 and actions.count("call.recording.accessed") == 1
    # RLS: tenant B's session sees no rows of tenant A at all, even with raw SQL.
    assert vh.sql("SELECT id FROM transcript_segments", tenant=tenant_b) == []
    assert vh.sql("SELECT id FROM recordings") == []


# ----------------------------------------------------------------------------------------- post-processing
def test_postprocess_sends_transcript_to_ai_agent_once(vh):
    tenant = uuid.uuid4()
    call_id, session_id = vh.seed_session(tenant)
    route = vh.mock.post("http://ai-agent-service:8000/internal/v1/calls/postprocess").mock(
        return_value=httpx.Response(200, json={"summary": "s", "outcome": "booked", "qualified": True})
    )

    async def go(fn):
        deps = vh.deps()
        try:
            return await fn(deps)
        finally:
            await deps.db.dispose()

    ctx = JobContext(tenant_id=tenant, correlation_id=uuid.uuid4())
    assert asyncio.run(go(lambda d: postprocess_session(d, ctx, session_id))) == "done"
    sent = json.loads(route.calls.last.request.content)
    assert sent["call_id"] == str(call_id) and sent["transcript"][1] == {
        "role": "caller",
        "text": "Book me for Tuesday.",
    }
    assert route.calls.last.request.headers["X-Tenant-ID"] == str(tenant)
    assert asyncio.run(go(lambda d: postprocess_session(d, ctx, session_id))) == "skipped"
    assert route.call_count == 1
    # A transient upstream failure is retried (not marked failed).
    _, second = vh.seed_session(tenant)
    route.mock(return_value=httpx.Response(503, json={"code": "unavailable"}))
    with pytest.raises(TransientFailure):
        asyncio.run(go(lambda d: postprocess_session(d, ctx, second)))
    assert (
        vh.sql("SELECT postprocess_status FROM voice_sessions WHERE id = :i", {"i": str(second)}, tenant=tenant)[0][
            "postprocess_status"
        ]
        == "queued"
    )


def test_retention_purges_old_transcripts_and_recordings(vh):
    tenant = uuid.uuid4()
    old_call, old_session = vh.seed_session(tenant, ended_days_ago=400)
    new_call, new_session = vh.seed_session(tenant, ended_days_ago=1)
    vh.mock.get(f"http://tenant-service:8000/internal/v1/tenants/{tenant}/settings/retention").mock(
        return_value=httpx.Response(200, json={"recordings_days": 30, "transcripts_days": 90})
    )
    # Sessions of other tests' tenants are also scanned: they get generous defaults.
    vh.mock.get(url__regex=r"^http://tenant-service:8000/internal/v1/tenants/[^/]+/settings/retention$").mock(
        return_value=httpx.Response(200, json={"recordings_days": 3650, "transcripts_days": 3650})
    )

    async def go():
        deps = vh.deps()
        try:
            return await retention_purge(deps)
        finally:
            await deps.db.dispose()

    assert asyncio.run(go()) >= 2
    assert (
        vh.sql("SELECT id FROM transcript_segments WHERE session_id = :s", {"s": str(old_session)}, tenant=tenant) == []
    )
    assert (
        len(vh.sql("SELECT id FROM transcript_segments WHERE session_id = :s", {"s": str(new_session)}, tenant=tenant))
        == 3
    )
    old_rec = vh.sql("SELECT status FROM recordings WHERE call_id = :c", {"c": str(old_call)}, tenant=tenant)
    assert old_rec[0]["status"] == "deleted"
    assert ("recordings", tenant_key(tenant, "calls", str(old_call), "recording.wav")) not in vh.store.objects
    assert ("recordings", tenant_key(tenant, "calls", str(new_call), "recording.wav")) in vh.store.objects
    auditor = vh.user(tenant, {"calls:recordings:read"})
    assert vh.client.get(f"/api/v1/voice/calls/{old_call}/transcript", headers=auth(auditor, tenant)).status_code == 404
    assert vh.client.get(f"/api/v1/voice/calls/{old_call}/recording", headers=auth(auditor, tenant)).status_code == 404
    done = vh.sql("SELECT retention_done FROM session_directory WHERE session_id = :s", {"s": str(old_session)})
    assert done[0]["retention_done"] is True
