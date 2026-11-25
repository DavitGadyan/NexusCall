"""Outbound email adapters implementing `nexus_providers.email.EmailProvider` (+ reconcile/identity extras).

* `SesV2Provider` (real): Amazon SES v2 `SendEmail` with raw MIME built by `email_service.mime`, through
  aiobotocore. botocore's own retries are disabled for sends: a retry after a read timeout could deliver
  twice. Read timeouts and dropped connections raise `AmbiguousSendError`; the caller reconciles instead
  of retrying. SES has no lookup-by-key API, so `supports_lookup` is False and reconciliation relies on the
  SES event stream (the `nexus_message_id` tag).
* `SimulatorSesProvider` (fake, development/test only): HTTP to the fake-providers SES simulator, which
  accepts sends idempotently (`X-Idempotency-Key`) and can be asked whether a key was already accepted.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Protocol

import httpx

from email_service.mime import build_mime
from nexus_core.config import ServiceSettings
from nexus_core.credentials import ResolvedCredentials
from nexus_core.providers import select_adapter
from nexus_providers.email import (
    EmailCredentials,
    OutboundEmail,
    PermanentSendError,
    SendResult,
    TransientSendError,
)

TAG_MESSAGE_ID = "nexus_message_id"
TAG_CATEGORY = "category"


class AmbiguousSendError(TransientSendError):
    """The request may have reached the provider. Never resend without reconciling first."""


class OutboundProvider(Protocol):
    name: str
    supports_lookup: bool

    async def send(self, creds: EmailCredentials, message: OutboundEmail) -> SendResult: ...

    async def lookup(self, creds: EmailCredentials, idempotency_key: str) -> str | None: ...

    async def create_domain_identity(self, creds: EmailCredentials, domain: str) -> list[str]: ...

    async def domain_status(self, creds: EmailCredentials, domain: str) -> dict[str, str]: ...


# SES v2 error codes that will not succeed on retry.
_SES_PERMANENT = frozenset(
    {
        "MessageRejected",
        "MailFromDomainNotVerifiedException",
        "AccountSuspendedException",
        "SendingPausedException",
        "NotFoundException",
        "BadRequestException",
        "AccessDeniedException",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
    }
)


def _tags(message: OutboundEmail) -> list[dict[str, str]]:
    return [
        {"Name": TAG_CATEGORY, "Value": message.category},
        {"Name": TAG_MESSAGE_ID, "Value": message.idempotency_key},
    ]


def _from(message: OutboundEmail) -> str:
    from email.utils import formataddr

    return formataddr((message.from_name, message.from_address)) if message.from_name else message.from_address


def _classify(code: str, status: int) -> type[TransientSendError] | type[PermanentSendError]:
    if code in _SES_PERMANENT or (400 <= status < 500 and status != 429 and "Throttl" not in code):
        return PermanentSendError
    return TransientSendError


class SesV2Provider:
    name = "ses"
    supports_lookup = False

    def __init__(
        self,
        *,
        configuration_set: str | None = None,
        timeout_seconds: float = 10.0,
        endpoint_url: str | None = None,
        http_session_cls: Any = None,
    ) -> None:
        self._configuration_set = configuration_set
        self._timeout = timeout_seconds
        self._endpoint_url = endpoint_url
        self._http_session_cls = http_session_cls

    def _client(self, creds: EmailCredentials, *, retries: int) -> Any:
        from aiobotocore.config import AioConfig
        from aiobotocore.session import get_session

        options: dict[str, Any] = {
            "connect_timeout": self._timeout,
            "read_timeout": self._timeout,
            # botocore: `total_max_attempts` counts the first request (`max_attempts` would mean extra retries).
            "retries": {"total_max_attempts": retries, "mode": "standard"},
        }
        if self._http_session_cls is not None:
            options["http_session_cls"] = self._http_session_cls
        return get_session().create_client(
            "sesv2",
            region_name=creds.region,
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            endpoint_url=self._endpoint_url,
            config=AioConfig(**options),
        )

    async def send(self, creds: EmailCredentials, message: OutboundEmail) -> SendResult:
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            HTTPClientError,
            ReadTimeoutError,
        )

        params: dict[str, Any] = {
            "FromEmailAddress": _from(message),
            "Destination": {"ToAddresses": [message.to_address]},
            "Content": {"Raw": {"Data": build_mime(message)}},
            "EmailTags": _tags(message),
        }
        if message.configuration_set or self._configuration_set:
            params["ConfigurationSetName"] = message.configuration_set or self._configuration_set
        try:
            async with self._client(creds, retries=1) as ses:
                resp = await ses.send_email(**params)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
            raise _classify(code, status)(code) from exc
        except (ConnectTimeoutError, EndpointConnectionError) as exc:  # never reached SES
            raise TransientSendError(type(exc).__name__) from exc
        except (ReadTimeoutError, ConnectionClosedError, HTTPClientError, TimeoutError) as exc:
            raise AmbiguousSendError(type(exc).__name__) from exc
        except (BotoCoreError, OSError) as exc:
            raise AmbiguousSendError(type(exc).__name__) from exc
        return SendResult(provider_message_id=str(resp["MessageId"]))

    async def lookup(self, creds: EmailCredentials, idempotency_key: str) -> str | None:
        return None  # SES v2 cannot look a message up by client key; reconciliation uses SES events.

    async def create_domain_identity(self, creds: EmailCredentials, domain: str) -> list[str]:
        from botocore.exceptions import ClientError

        async with self._client(creds, retries=3) as ses:
            try:
                resp = await ses.create_email_identity(EmailIdentity=domain)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "AlreadyExistsException":
                    raise
                resp = await ses.get_email_identity(EmailIdentity=domain)
        return [str(t) for t in resp.get("DkimAttributes", {}).get("Tokens", [])]

    async def domain_status(self, creds: EmailCredentials, domain: str) -> dict[str, str]:
        async with self._client(creds, retries=3) as ses:
            resp = await ses.get_email_identity(EmailIdentity=domain)
        return {
            "verified_for_sending": str(bool(resp.get("VerifiedForSendingStatus"))).lower(),
            "dkim_status": str(resp.get("DkimAttributes", {}).get("Status", "NOT_STARTED")),
            "identity_type": str(resp.get("IdentityType", "DOMAIN")),
        }


class SimulatorSesProvider:
    """DEVELOPMENT/TEST ONLY: talks to `fake_providers.routers.ses`."""

    name = "fake"
    supports_lookup = True

    def __init__(
        self, base_url: str, *, timeout_seconds: float = 10.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = base_url.rstrip("/") + "/ses"
        self._timeout = timeout_seconds
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport, follow_redirects=False)

    async def send(self, creds: EmailCredentials, message: OutboundEmail) -> SendResult:
        body: dict[str, Any] = {
            "FromEmailAddress": _from(message),
            "Destination": {"ToAddresses": [message.to_address]},
            "Content": {"Raw": {"Data": base64.b64encode(build_mime(message)).decode()}},
            "EmailTags": _tags(message),
        }
        if message.configuration_set:
            body["ConfigurationSetName"] = message.configuration_set
        try:
            async with self._http() as client:
                resp = await client.post(
                    f"{self._base}/v2/email/outbound-emails",
                    json=body,
                    headers={"X-Idempotency-Key": message.idempotency_key},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TransientSendError(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise AmbiguousSendError(type(exc).__name__) from exc
        if resp.status_code != 200:
            code = resp.headers.get("x-amzn-ErrorType") or f"http_{resp.status_code}"
            raise _classify(code, resp.status_code)(code)
        return SendResult(provider_message_id=str(resp.json()["MessageId"]))

    async def lookup(self, creds: EmailCredentials, idempotency_key: str) -> str | None:
        try:
            async with self._http() as client:
                resp = await client.get(f"{self._base}/_idempotency/{idempotency_key}")
        except httpx.HTTPError as exc:
            raise TransientSendError(type(exc).__name__) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise TransientSendError(f"http_{resp.status_code}")
        return str(resp.json()["MessageId"])

    async def create_domain_identity(self, creds: EmailCredentials, domain: str) -> list[str]:
        async with self._http() as client:
            resp = await client.post(f"{self._base}/v2/email/identities", json={"EmailIdentity": domain})
        resp.raise_for_status()
        return [str(t) for t in resp.json()["DkimAttributes"]["Tokens"]]

    async def domain_status(self, creds: EmailCredentials, domain: str) -> dict[str, str]:
        async with self._http() as client:
            resp = await client.get(f"{self._base}/v2/email/identities/{domain}")
        resp.raise_for_status()
        data = resp.json()
        return {
            "verified_for_sending": str(bool(data.get("VerifiedForSendingStatus"))).lower(),
            "dkim_status": str(data.get("DkimAttributes", {}).get("Status", "NOT_STARTED")),
            "identity_type": "DOMAIN",
        }


def fake_dkim_tokens(domain: str) -> list[str]:
    """Deterministic DKIM tokens used by the simulator (and the fake DNS verifier accepts them)."""
    return [hashlib.sha256(f"{domain}:{i}".encode()).hexdigest()[:32] for i in range(3)]


def to_email_credentials(resolved: ResolvedCredentials) -> EmailCredentials:
    if resolved.source == "fake":
        return EmailCredentials(region="local", access_key_id="fake", secret_access_key="fake")  # noqa: S106
    return EmailCredentials(
        region=resolved.secret("region"),
        access_key_id=resolved.secret("access_key_id"),
        secret_access_key=resolved.secret("secret_access_key"),
    )


def build_outbound_provider(settings: ServiceSettings, *, configuration_set: str | None, timeout: float) -> Any:
    return select_adapter(
        settings,
        real=lambda: SesV2Provider(configuration_set=configuration_set, timeout_seconds=timeout),
        fake=lambda: SimulatorSesProvider(settings.fake_providers_url, timeout_seconds=timeout),
        name="email",
    )
