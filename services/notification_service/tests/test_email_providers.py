"""Email adapter contract tests: SES v2 against mocked HTTP, the Mailpit fake against an in-process SMTP
server. Both implement `nexus_providers.email.EmailProvider` and map errors the same way."""

from __future__ import annotations

import asyncio
import email
import json
import socket

import httpx2
import pytest
from aiobotocore.httpxsession import HttpxSession

from nexus_core.config import Environment, ServiceSettings
from nexus_core.providers import FakeProviderInProduction
from nexus_providers.email import EmailCredentials, OutboundEmail, PermanentSendError, TransientSendError
from notification_service.providers.email import MailpitEmailProvider, SesEmailProvider, build_email_provider

CREDS = EmailCredentials(region="us-east-1", access_key_id="AKIDEXAMPLE", secret_access_key="secret-example")
SES_HOST = "email.us-east-1.amazonaws.com"


def _message(to="ada@acme.test") -> OutboundEmail:
    return OutboundEmail(
        from_address="no-reply@nexus.test",
        from_name="NexusCall",
        to_address=to,
        subject="You're invited",
        text="Accept: https://app.test/x",
        html="<p>Accept</p>",
        category="transactional",
        idempotency_key="k1",
    )


class MockSes:
    """Mocked HTTP for aiobotocore: its httpx session gets an in-memory transport (no network at all)."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == SES_HOST
        self.requests.append(request)
        return self.respond(request)

    def provider(self) -> SesEmailProvider:
        mock = self

        class _Session(HttpxSession):
            def _make_async_client(self):  # type: ignore[override]
                return httpx2.AsyncClient(transport=httpx2.MockTransport(mock.handler))

        return SesEmailProvider(configuration_set="ops", timeout_seconds=2, http_session_cls=_Session)


# --------------------------------------------------------------------------- SES (real adapter, mocked HTTP)
async def test_ses_send_builds_the_v2_request_and_returns_the_message_id():
    ses = MockSes(lambda r: httpx2.Response(200, json={"MessageId": "0100-abc"}))
    result = await ses.provider().send(CREDS, _message())
    assert result.provider_message_id == "0100-abc"
    [req] = ses.requests
    assert req.method == "POST" and req.url.path == "/v2/email/outbound-emails"
    body = json.loads(req.content)
    assert body["FromEmailAddress"] == "NexusCall <no-reply@nexus.test>"
    assert body["Destination"] == {"ToAddresses": ["ada@acme.test"]}
    assert body["Content"]["Simple"]["Subject"]["Data"] == "You're invited"
    assert body["Content"]["Simple"]["Body"]["Html"]["Data"] == "<p>Accept</p>"
    assert body["ConfigurationSetName"] == "ops"
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
async def test_ses_errors_map_to_permanent_or_transient(status, code, expected):
    ses = MockSes(lambda r: httpx2.Response(status, headers={"x-amzn-ErrorType": code}, json={"message": "nope"}))
    with pytest.raises(expected) as info:
        await ses.provider().send(CREDS, _message())
    assert str(info.value) == code and "secret-example" not in str(info.value)


async def test_ses_network_failure_is_transient():
    def boom(request):
        raise httpx2.ConnectTimeout("timeout", request=request)

    with pytest.raises(TransientSendError):
        await MockSes(boom).provider().send(CREDS, _message())


async def test_ses_domain_status():
    ses = MockSes(
        lambda r: httpx2.Response(
            200,
            json={"IdentityType": "DOMAIN", "VerifiedForSendingStatus": True, "DkimAttributes": {"Status": "SUCCESS"}},
        )
    )
    status = await ses.provider().domain_status(CREDS, "acme.test")
    assert ses.requests[0].url.path == "/v2/email/identities/acme.test"
    assert status == {"verified_for_sending": "true", "dkim_status": "SUCCESS", "identity_type": "DOMAIN"}


# --------------------------------------------------------------------------- Mailpit fake (SMTP)
class SmtpSink:
    """Tiny SMTP server: enough of RFC 5321 for smtplib. Recipients containing 'reject' get a 550."""

    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.server: asyncio.base_events.Server | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 sink ESMTP\r\n")
        while line := await reader.readline():
            cmd = line.decode().strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                writer.write(b"250-sink\r\n250 OK\r\n")
            elif cmd.startswith("RCPT") and "REJECT" in cmd:
                writer.write(b"550 no such user\r\n")
            elif cmd == "DATA":
                writer.write(b"354 go ahead\r\n")
                await writer.drain()
                data = await reader.readuntil(b"\r\n.\r\n")
                self.messages.append(data[:-5])
                writer.write(b"250 queued\r\n")
            elif cmd == "QUIT":
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 OK\r\n")
            await writer.drain()
        writer.close()

    async def __aenter__(self) -> int:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


async def test_mailpit_fake_sends_multipart_text_and_html():
    sink = SmtpSink()
    async with sink as port:
        result = await MailpitEmailProvider("127.0.0.1", port, timeout_seconds=5).send(CREDS, _message())
    assert len(sink.messages) == 1
    msg = email.message_from_bytes(sink.messages[0])
    assert msg["To"] == "ada@acme.test" and msg["Subject"] == "You're invited"
    assert msg["Message-ID"] == result.provider_message_id
    assert msg["Auto-Submitted"] == "auto-generated"
    types = [part.get_content_type() for part in msg.walk()]
    assert "text/plain" in types and "text/html" in types


async def test_mailpit_fake_error_mapping():
    sink = SmtpSink()
    async with sink as port:
        with pytest.raises(PermanentSendError):
            await MailpitEmailProvider("127.0.0.1", port, timeout_seconds=5).send(CREDS, _message("reject@acme.test"))
    with socket.socket() as s:  # a port with nothing listening
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    with pytest.raises(TransientSendError):
        await MailpitEmailProvider("127.0.0.1", closed_port, timeout_seconds=2).send(CREDS, _message())


def test_adapter_selection_and_production_guard():
    dev = ServiceSettings(env="development", provider_mode="fake")
    assert isinstance(build_email_provider(dev, mailpit_hostport="mailpit:1025", configuration_set=None,
                                           timeout_seconds=1), MailpitEmailProvider)  # fmt: skip
    real = ServiceSettings(env="production", provider_mode="real")
    assert isinstance(build_email_provider(real, mailpit_hostport="x:1", configuration_set=None,
                                           timeout_seconds=1), SesEmailProvider)  # fmt: skip
    sneaky = ServiceSettings(env="development", provider_mode="fake").model_copy(update={"env": Environment.PRODUCTION})
    with pytest.raises(FakeProviderInProduction):
        build_email_provider(sneaky, mailpit_hostport="x:1", configuration_set=None, timeout_seconds=1)
    assert MailpitEmailProvider.from_hostport("mailpit:1025")._port == 1025
