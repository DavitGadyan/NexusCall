"""Provider adapter contract tests. SES v2 runs against an in-memory httpx2 transport (aiobotocore's HTTP
client; respx does not intercept it), the simulator adapter against the fake-providers router mounted
in-process. Nothing here opens a connection to a real provider."""

from __future__ import annotations

import asyncio
import base64
import email
import json
from email import policy as email_policy

import httpx
import httpx2
import pytest
import respx
from aiobotocore.httpxsession import HttpxSession
from fastapi import FastAPI

from email_service.providers.ses import (
    AmbiguousSendError,
    SesV2Provider,
    SimulatorSesProvider,
    build_outbound_provider,
    fake_dkim_tokens,
)
from fake_providers.routers import ses as fake_ses
from fake_providers.sns import cert_url, public_key
from nexus_core.config import Environment, ServiceSettings
from nexus_core.providers import FakeProviderInProduction
from nexus_core.webhooks import SnsVerifier
from nexus_providers.email import EmailCredentials, OutboundEmail, PermanentSendError, TransientSendError

CREDS = EmailCredentials(region="us-east-1", access_key_id="AKIDEXAMPLE", secret_access_key="secret-example")
SES_HOST = "email.us-east-1.amazonaws.com"


def _message(to: str = "ada@example.test", key: str = "8a1d0c6e-0000-4000-8000-000000000001") -> OutboundEmail:
    return OutboundEmail(
        from_address="news@acme.test",
        from_name="Acme",
        to_address=to,
        subject="Spring offer",
        text="Hi Ada",
        html="<p>Hi Ada</p>",
        headers={
            "Message-ID": "<m1@acme.test>",
            "List-Unsubscribe": "<https://app.test/u/tok>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        category="campaign",
        idempotency_key=key,
    )


class MockSes:
    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == SES_HOST
        self.requests.append(request)
        return self.respond(request)

    def provider(self) -> SesV2Provider:
        mock = self

        class _Session(HttpxSession):
            def _make_async_client(self):  # type: ignore[override]
                return httpx2.AsyncClient(transport=httpx2.MockTransport(mock.handler))

        return SesV2Provider(configuration_set="nexus-events", timeout_seconds=2, http_session_cls=_Session)


# ----------------------------------------------------------------------------------------- SES v2 (real)
async def test_ses_sends_raw_mime_with_tags_and_configuration_set():
    ses = MockSes(lambda r: httpx2.Response(200, json={"MessageId": "0100-abc"}))
    result = await ses.provider().send(CREDS, _message())
    assert result.provider_message_id == "0100-abc"
    [req] = ses.requests
    assert req.method == "POST" and req.url.path == "/v2/email/outbound-emails"
    body = json.loads(req.content)
    assert body["ConfigurationSetName"] == "nexus-events"
    assert body["Destination"] == {"ToAddresses": ["ada@example.test"]}
    assert {"Name": "nexus_message_id", "Value": _message().idempotency_key} in body["EmailTags"]
    mime = email.message_from_bytes(base64.b64decode(body["Content"]["Raw"]["Data"]), policy=email_policy.default)
    assert mime["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and mime["Message-ID"] == "<m1@acme.test>"
    assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "secret-example" not in req.headers["authorization"]


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "MessageRejected", PermanentSendError),
        (400, "MailFromDomainNotVerifiedException", PermanentSendError),
        (403, "AccessDeniedException", PermanentSendError),
        (429, "TooManyRequestsException", TransientSendError),
        (500, "InternalFailure", TransientSendError),
    ],
)
async def test_ses_error_mapping(status, code, expected):
    ses = MockSes(lambda r: httpx2.Response(status, headers={"x-amzn-ErrorType": code}, json={"message": "no"}))
    with pytest.raises(expected) as info:
        await ses.provider().send(CREDS, _message())
    assert not isinstance(info.value, AmbiguousSendError)
    assert str(info.value) == code and "secret-example" not in str(info.value)


async def test_ses_read_timeout_is_ambiguous_and_never_retried():
    def slow(request):
        raise httpx2.ReadTimeout("timeout", request=request)

    ses = MockSes(slow)
    with pytest.raises(AmbiguousSendError):
        await ses.provider().send(CREDS, _message())
    assert len(ses.requests) == 1  # botocore's own retry is off for sends: no blind second attempt


async def test_ses_connect_failure_is_plain_transient():
    def refused(request):
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(TransientSendError) as info:
        await MockSes(refused).provider().send(CREDS, _message())
    assert not isinstance(info.value, AmbiguousSendError)


async def test_ses_domain_identity_returns_dkim_tokens():
    ses = MockSes(lambda r: httpx2.Response(200, json={"IdentityType": "DOMAIN",
                                                         "DkimAttributes": {"Tokens": ["a", "b", "c"]}}))  # fmt: skip
    assert await ses.provider().create_domain_identity(CREDS, "acme.test") == ["a", "b", "c"]
    assert ses.requests[0].url.path == "/v2/email/identities"
    assert SesV2Provider().supports_lookup is False and await SesV2Provider().lookup(CREDS, "k") is None


def test_adapter_selection_and_production_guard():
    dev = ServiceSettings(env="development", provider_mode="fake")
    assert isinstance(build_outbound_provider(dev, configuration_set=None, timeout=1), SimulatorSesProvider)
    real = ServiceSettings(env="production", provider_mode="real")
    assert isinstance(build_outbound_provider(real, configuration_set=None, timeout=1), SesV2Provider)
    sneaky = ServiceSettings(env="development", provider_mode="fake").model_copy(update={"env": Environment.PRODUCTION})
    with pytest.raises(FakeProviderInProduction):
        build_outbound_provider(sneaky, configuration_set=None, timeout=1)


# ----------------------------------------------------------------------------------- simulator (fake)
@pytest.fixture
def simulator(monkeypatch):
    monkeypatch.setenv("MAILPIT_SMTP_HOST", "off")
    monkeypatch.delenv("EMAIL_EVENTS_WEBHOOK_URL", raising=False)
    fake_ses.reset()
    app = FastAPI()
    app.include_router(fake_ses.router)
    return SimulatorSesProvider("http://fake-providers:8000", transport=httpx.ASGITransport(app=app))


async def test_simulator_accepts_sends_idempotently_and_reconciles(simulator):
    first = await simulator.send(CREDS, _message())
    again = await simulator.send(CREDS, _message())
    assert first.provider_message_id == again.provider_message_id
    assert len(fake_ses.STATE.sent) == 1  # relayed and evented once
    assert await simulator.lookup(CREDS, _message().idempotency_key) == first.provider_message_id
    assert await simulator.lookup(CREDS, "never-sent") is None
    assert simulator.supports_lookup is True


async def test_simulator_reject_domain_is_permanent(simulator):
    with pytest.raises(PermanentSendError):
        await simulator.send(CREDS, _message(to="x@reject.example.test"))


async def test_simulator_identities_are_deterministic(simulator):
    assert await simulator.create_domain_identity(CREDS, "acme.test") == fake_dkim_tokens("acme.test")
    assert (await simulator.domain_status(CREDS, "acme.test"))["verified_for_sending"] == "true"
    assert (await simulator.domain_status(CREDS, "unverified.test"))["verified_for_sending"] == "false"


@pytest.mark.parametrize(
    ("to", "kinds"),
    [
        ("ada@example.test", ["Delivery"]),
        ("ada@bounce.example.test", ["Bounce"]),
        ("ada@complaint.example.test", ["Delivery", "Complaint"]),
    ],
)
async def test_simulator_posts_signed_sns_events(simulator, monkeypatch, to, kinds):
    monkeypatch.setenv("EMAIL_EVENTS_WEBHOOK_URL", "http://email-service:8000/webhooks/ses/events")
    monkeypatch.setenv("FAKE_SES_EVENT_DELAY_SECONDS", "0")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://email-service:8000/webhooks/ses/events").mock(return_value=httpx.Response(200))
        result = await simulator.send(CREDS, _message(to=to, key=f"k-{to}"))
        await asyncio.gather(*list(fake_ses.STATE.tasks))
    envelopes = [json.loads(call.request.content) for call in route.calls]
    events = [json.loads(e["Message"]) for e in envelopes]
    assert [e["eventType"] for e in events] == kinds
    assert all(e["mail"]["messageId"] == result.provider_message_id for e in events)
    assert events[0]["mail"]["tags"]["nexus_message_id"] == [f"k-{to}"]
    verifier = SnsVerifier(r"^fake-providers$", frozenset({fake_ses.DEFAULT_TOPIC}), allow_http=True)
    verifier._certs[cert_url()] = public_key()
    for envelope in envelopes:
        await verifier.verify(envelope)


async def test_simulator_relays_to_mailpit_over_smtp(simulator, monkeypatch):
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 sink\r\n")
        while line := await reader.readline():
            cmd = line.decode().strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                writer.write(b"250-sink\r\n250 OK\r\n")
            elif cmd == "DATA":
                writer.write(b"354 go\r\n")
                await writer.drain()
                received.append(await reader.readuntil(b"\r\n.\r\n"))
                writer.write(b"250 queued\r\n")
            elif cmd == "QUIT":
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 OK\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("MAILPIT_SMTP_HOST", f"127.0.0.1:{port}")
    try:
        await simulator.send(CREDS, _message(key="smtp-1"))
        await asyncio.gather(*list(fake_ses.STATE.tasks))
    finally:
        server.close()
        await server.wait_closed()
    assert len(received) == 1 and b"Subject: Spring offer" in received[0]
