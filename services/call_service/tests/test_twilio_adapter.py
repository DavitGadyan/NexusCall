"""Telephony adapter contract tests: the same `TwilioRest` client against the Twilio simulator (in-process ASGI)
and against mocked Twilio HTTP (respx). No request ever leaves the process."""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from call_service.deps import FAKE_ACCOUNT_SID
from call_service.providers.twilio import NotSent, ProviderCallNotFound, ProviderThrottled, TwilioRest
from nexus_providers.telephony import (
    AmbiguousProviderResult,
    CreateCallRequest,
    ProviderRejected,
    TelephonyCredentials,
)

SECRET = "adapter-test-secret"
REAL_SID = "AC" + "a" * 32
REAL = TelephonyCredentials(account_sid=REAL_SID, auth_token="real-token")


def _req(to: str = "+12125550123", key: str = "call-1:1") -> CreateCallRequest:
    return CreateCallRequest(
        to=to,
        from_="+12125550100",
        status_callback_url="https://hooks.example.test/webhooks/twilio/status?c=1",
        answer_url="https://hooks.example.test/webhooks/twilio/answer?c=1",
        idempotency_key=key,
    )


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("FAKE_PROVIDER_SECRET", SECRET)
    monkeypatch.setenv("FAKE_TWILIO_AUTOPROGRESS", "0")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    from fake_providers.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    return TwilioRest("http://fake-providers/twilio", transport=transport, token_lookup=True)


FAKE = TelephonyCredentials(account_sid=FAKE_ACCOUNT_SID, auth_token=SECRET)


# ----------------------------------------------------------------------------------------- against the fake
async def test_fake_create_is_idempotent_and_reconcilable(fake):
    first = await fake.create_call(FAKE, _req(key="k-1:1"))
    again = await fake.create_call(FAKE, _req(key="k-1:1"))
    assert first.provider_call_id == again.provider_call_id and first.status == "queued"
    other = await fake.create_call(FAKE, _req(key="k-2:1"))
    assert other.provider_call_id != first.provider_call_id
    found = await fake.find_call(
        FAKE, idempotency_key="k-1:1", to="+12125550123", from_="+12125550100", created_after=datetime.now(UTC)
    )
    assert found is not None and found.provider_call_id == first.provider_call_id
    assert await fake.find_call_by_idempotency_key(FAKE, "never-sent:1") is None
    fetched = await fake.get_call(FAKE, first.provider_call_id)
    assert fetched.provider_call_id == first.provider_call_id
    await fake.hangup(FAKE, first.provider_call_id)
    with pytest.raises(ProviderCallNotFound):
        await fake.get_call(FAKE, "CA" + "0" * 32)


async def test_fake_rejects_bad_credentials_and_numbers(fake):
    with pytest.raises(ProviderRejected):
        await fake.create_call(TelephonyCredentials(account_sid=FAKE_ACCOUNT_SID, auth_token="wrong"), _req())
    with pytest.raises(ProviderRejected):
        await fake.create_call(FAKE, _req(to="12345"))


async def test_fake_ambiguous_create_still_creates_the_call(fake):
    with pytest.raises(AmbiguousProviderResult):
        await fake.create_call(FAKE, _req(to="+12125550197", key="amb-1:1"))
    found = await fake.find_call_by_idempotency_key(FAKE, "amb-1:1")
    assert found is not None  # reconciliation finds it, so the dispatcher must not dial again


# ----------------------------------------------------------------------------------------- real REST (mocked)
BASE = "https://api.twilio.test"
CALLS_URL = f"{BASE}/2010-04-01/Accounts/{REAL_SID}/Calls.json"


@respx.mock
async def test_real_create_sends_documented_form_auth_and_token():
    route = respx.post(CALLS_URL).mock(
        return_value=httpx.Response(201, json={"sid": "CA" + "b" * 32, "status": "queued"})
    )
    call = await TwilioRest(BASE).create_call(REAL, _req(key="c:1"))
    assert call.provider_call_id == "CA" + "b" * 32
    sent = route.calls.last.request
    form = urllib.parse.parse_qs(sent.content.decode())
    assert form["To"] == ["+12125550123"] and form["From"] == ["+12125550100"]
    assert form["Url"][0].endswith("/answer?c=1") and form["StatusCallbackMethod"] == ["POST"]
    assert set(form["StatusCallbackEvent"]) == {"initiated", "ringing", "answered", "completed"}
    assert sent.headers["I-Twilio-Idempotency-Token"] == "c:1"
    assert sent.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_real_error_semantics():
    adapter = TwilioRest(BASE)
    respx.post(CALLS_URL).mock(return_value=httpx.Response(503, json={"code": 20503}))
    with pytest.raises(AmbiguousProviderResult):
        await adapter.create_call(REAL, _req())
    respx.post(CALLS_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AmbiguousProviderResult):
        await adapter.create_call(REAL, _req())
    respx.post(CALLS_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NotSent):
        await adapter.create_call(REAL, _req())
    respx.post(CALLS_URL).mock(return_value=httpx.Response(400, json={"code": 21211}))
    with pytest.raises(ProviderRejected, match="twilio_21211"):
        await adapter.create_call(REAL, _req())
    respx.post(CALLS_URL).mock(return_value=httpx.Response(429, json={"code": 20429}))
    with pytest.raises(ProviderThrottled):
        await adapter.create_call(REAL, _req())
    with pytest.raises(AmbiguousProviderResult):  # real Twilio cannot be searched by token
        await adapter.find_call_by_idempotency_key(REAL, "c:1")


@respx.mock
async def test_real_reconciliation_lookup_by_number_and_time_window():
    adapter = TwilioRest(BASE)
    created = datetime.now(UTC)

    def item(sid: str, when: datetime) -> dict[str, str]:
        return {
            "sid": sid,
            "status": "ringing",
            "direction": "outbound-api",
            "date_created": when.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        }

    old = item("CA" + "1" * 32, created - timedelta(hours=2))
    new = item("CA" + "2" * 32, created + timedelta(seconds=1))
    route = respx.get(CALLS_URL).mock(return_value=httpx.Response(200, json={"calls": [new, old]}))
    found = await adapter.find_call(REAL, idempotency_key="x:1", to="+1", from_="+2", created_after=created)
    assert found is not None and found.provider_call_id == "CA" + "2" * 32
    assert route.calls.last.request.url.params["To"] == "+1"
    route.mock(return_value=httpx.Response(200, json={"calls": [old]}))
    assert await adapter.find_call(REAL, idempotency_key="x:1", to="+1", from_="+2", created_after=created) is None
    other = item("CA" + "3" * 32, created + timedelta(seconds=2))
    route.mock(return_value=httpx.Response(200, json={"calls": [new, other]}))
    with pytest.raises(AmbiguousProviderResult):  # two candidates: never guess
        await adapter.find_call(REAL, idempotency_key="x:1", to="+1", from_="+2", created_after=created)


@respx.mock
async def test_real_hangup_and_transfer():
    sid = "CA" + "c" * 32
    route = respx.post(f"{BASE}/2010-04-01/Accounts/{REAL_SID}/Calls/{sid}.json").mock(
        return_value=httpx.Response(200, json={"sid": sid, "status": "completed"})
    )
    adapter = TwilioRest(BASE)
    await adapter.hangup(REAL, sid)
    assert urllib.parse.parse_qs(route.calls.last.request.content.decode())["Status"] == ["completed"]
    await adapter.transfer_to_conference(REAL, sid, "handoff-1")
    twiml = urllib.parse.parse_qs(route.calls.last.request.content.decode())["Twiml"][0]
    assert "<Conference" in twiml and "handoff-1" in twiml
    with pytest.raises(ProviderRejected):
        await adapter.hangup(REAL, "not-a-sid")
    with pytest.raises(ProviderRejected):
        await adapter.create_call(TelephonyCredentials(account_sid="bad", auth_token="t"), _req())
