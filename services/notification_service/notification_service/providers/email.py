"""Operational email adapters implementing `nexus_providers.email.EmailProvider`.

* `SesEmailProvider` (real): Amazon SES v2 `SendEmail` through aiobotocore, with connect/read timeouts and
  bounded retries. Platform credentials come from Vault (`platform/notification-service/ses`).
* `MailpitEmailProvider` (fake, development/test only): plain SMTP to Mailpit so developers can open the
  invitation links. Selected through `select_adapter`, which refuses fakes in staging/production.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from pydantic import SecretStr

from nexus_core.config import ServiceSettings
from nexus_core.providers import select_adapter
from nexus_core.vault import VaultClient, VaultNotFound, platform_secret_path
from nexus_providers.email import (
    EmailCredentials,
    EmailProvider,
    OutboundEmail,
    PermanentSendError,
    SendResult,
    TransientSendError,
)

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


class SesEmailProvider:
    name = "ses"

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

    def _client(self, creds: EmailCredentials) -> Any:
        from aiobotocore.config import AioConfig
        from aiobotocore.session import get_session

        options: dict[str, Any] = {
            "connect_timeout": self._timeout,
            "read_timeout": self._timeout,
            "retries": {"max_attempts": 2, "mode": "standard"},
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
        from botocore.exceptions import BotoCoreError, ClientError

        body: dict[str, Any] = {"Text": {"Data": message.text, "Charset": "UTF-8"}}
        if message.html:
            body["Html"] = {"Data": message.html, "Charset": "UTF-8"}
        params: dict[str, Any] = {
            "FromEmailAddress": formataddr((message.from_name, message.from_address))
            if message.from_name
            else message.from_address,
            "Destination": {"ToAddresses": [message.to_address]},
            "Content": {"Simple": {"Subject": {"Data": message.subject, "Charset": "UTF-8"}, "Body": body}},
            "EmailTags": [{"Name": "category", "Value": message.category}],
        }
        if message.reply_to:
            params["ReplyToAddresses"] = [message.reply_to]
        if message.configuration_set or self._configuration_set:
            params["ConfigurationSetName"] = message.configuration_set or self._configuration_set
        try:
            async with self._client(creds) as ses:
                resp = await ses.send_email(**params)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
            if code in _SES_PERMANENT or (400 <= status < 500 and status != 429 and "Throttl" not in code):
                raise PermanentSendError(code) from exc
            raise TransientSendError(code) from exc
        except (BotoCoreError, TimeoutError, OSError) as exc:
            raise TransientSendError(type(exc).__name__) from exc
        return SendResult(provider_message_id=str(resp["MessageId"]))

    async def domain_status(self, creds: EmailCredentials, domain: str) -> dict[str, str]:
        async with self._client(creds) as ses:
            resp = await ses.get_email_identity(EmailIdentity=domain)
        return {
            "verified_for_sending": str(bool(resp.get("VerifiedForSendingStatus"))).lower(),
            "dkim_status": str(resp.get("DkimAttributes", {}).get("Status", "NOT_STARTED")),
            "identity_type": str(resp.get("IdentityType", "DOMAIN")),
        }


class MailpitEmailProvider:
    """DEVELOPMENT/TEST ONLY: SMTP sink. Never selected when ENV is staging or production."""

    name = "fake"

    def __init__(self, host: str, port: int, *, timeout_seconds: float = 10.0) -> None:
        self._host, self._port, self._timeout = host, port, timeout_seconds

    @classmethod
    def from_hostport(cls, hostport: str, *, timeout_seconds: float = 10.0) -> MailpitEmailProvider:
        host, _, port = hostport.rpartition(":")
        return cls(host or hostport, int(port) if port.isdigit() else 1025, timeout_seconds=timeout_seconds)

    @staticmethod
    def build_message(message: OutboundEmail) -> EmailMessage:
        msg = EmailMessage()
        sender = message.from_address
        msg["From"] = formataddr((message.from_name, sender)) if message.from_name else sender
        msg["To"] = message.to_address
        msg["Subject"] = message.subject
        msg["Message-ID"] = make_msgid(domain="nexus.local")
        msg["Auto-Submitted"] = "auto-generated"
        msg["X-Nexus-Category"] = message.category
        if message.reply_to:
            msg["Reply-To"] = message.reply_to
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")
        return msg

    def _send_sync(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            smtp.send_message(msg)

    async def send(self, creds: EmailCredentials, message: OutboundEmail) -> SendResult:
        msg = self.build_message(message)
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise PermanentSendError("recipient_refused") from exc
        except smtplib.SMTPResponseException as exc:
            if 500 <= exc.smtp_code < 600:
                raise PermanentSendError(f"smtp_{exc.smtp_code}") from exc
            raise TransientSendError(f"smtp_{exc.smtp_code}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientSendError(type(exc).__name__) from exc
        return SendResult(provider_message_id=str(msg["Message-ID"]))

    async def domain_status(self, creds: EmailCredentials, domain: str) -> dict[str, str]:
        return {"verified_for_sending": "true", "dkim_status": "NOT_APPLICABLE", "identity_type": "DOMAIN"}


FAKE_CREDENTIALS = EmailCredentials(region="local", access_key_id="fake", secret_access_key="fake")  # noqa: S106


class PlatformSesCredentials:
    """Reads the platform SES credentials from Vault under this service's own identity; cached briefly."""

    CACHE_SECONDS = 60

    def __init__(self, vault: VaultClient | None, env: str) -> None:
        self._vault, self._path = vault, platform_secret_path(env, "notification-service/ses")
        self._cached: tuple[float, EmailCredentials] | None = None

    async def get(self) -> EmailCredentials:
        if self._cached and self._cached[0] > time.monotonic():
            return self._cached[1]
        if self._vault is None:
            raise TransientSendError("secrets_backend_not_configured")
        try:
            values: dict[str, SecretStr] = await self._vault.read(self._path)
        except VaultNotFound as exc:
            raise TransientSendError("ses_credentials_missing") from exc
        try:
            creds = EmailCredentials(
                region=values["region"].get_secret_value(),
                access_key_id=values["access_key_id"].get_secret_value(),
                secret_access_key=values["secret_access_key"].get_secret_value(),
            )
        except KeyError as exc:
            raise TransientSendError("ses_credentials_incomplete") from exc
        self._cached = (time.monotonic() + self.CACHE_SECONDS, creds)
        return creds


class StaticCredentials:
    def __init__(self, creds: EmailCredentials) -> None:
        self._creds = creds

    async def get(self) -> EmailCredentials:
        return self._creds


def build_email_provider(settings: ServiceSettings, *, mailpit_hostport: str, configuration_set: str | None,
                         timeout_seconds: float) -> EmailProvider:  # fmt: skip
    return select_adapter(
        settings,
        real=lambda: SesEmailProvider(configuration_set=configuration_set, timeout_seconds=timeout_seconds),
        fake=lambda: MailpitEmailProvider.from_hostport(mailpit_hostport, timeout_seconds=timeout_seconds),
        name="email",
    )
