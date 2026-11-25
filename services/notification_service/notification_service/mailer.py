"""Operational email delivery: render a code-defined template, rate-limit per recipient, send through the
selected provider, and record the outcome in the append-only `email_log` (hash of the recipient only).

Sending happens inside the internal request on purpose: template variables can contain bearer links
(invitations), which must never be written to the database, a queue, logs or audit metadata. The call is
short and bounded by the provider timeout; the caller records `delivery_failed` on a non-2xx response.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.errors import ProblemError, ServiceUnavailable, TooManyRequests, Unprocessable
from nexus_core.ratelimit import allow_fixed_window
from nexus_providers.email import (
    EmailCredentials,
    EmailProvider,
    OutboundEmail,
    PermanentSendError,
    TransientSendError,
)
from notification_service.tables import email_log
from notification_service.templates import render

log = logging.getLogger(__name__)
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
ADDRESS_RE = re.compile(rf"^[A-Za-z0-9.!#$%&'*+/=?^_`{{|}}~-]{{1,64}}@{_LABEL}(?:\.{_LABEL})+$")


class CredentialSource(Protocol):
    async def get(self) -> EmailCredentials: ...


class EmailRejected(ProblemError):
    status, code, title = 422, "email_rejected", "The email provider rejected the message"


def normalize_address(address: str) -> str:
    value = address.strip().lower()
    if len(value) > 254 or not ADDRESS_RE.match(value):
        raise Unprocessable("Invalid recipient address")
    return value


def recipient_hash(address: str) -> str:
    """Same convention as `EmailAddressEvent.address_hash`: SHA-256 of the normalized address."""
    return hashlib.sha256(normalize_address(address).encode()).hexdigest()


class Mailer:
    def __init__(
        self,
        provider: EmailProvider,
        credentials: CredentialSource,
        *,
        from_address: str,
        from_name: str | None,
        public_base_url: str,
        per_recipient_per_hour: int,
    ) -> None:
        self.provider = provider
        self._credentials = credentials
        self._from_address, self._from_name = from_address, from_name
        self._base = public_base_url
        self._limit = per_recipient_per_hour

    async def _log(
        self,
        db: Database,
        *,
        tenant_id: uuid.UUID | None,
        template: str,
        rhash: str,
        status: str,
        requested_by: str,
        provider_message_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        actor = Actor("service", requested_by)
        ctx = db.tenant_session(tenant_id, actor) if tenant_id else db.global_session(actor)
        async with ctx as s:
            # Explicit id: no implicit RETURNING, which platform-level rows (no tenant) could not satisfy,
            # because they are invisible to every SELECT policy of the runtime role.
            await s.execute(
                insert(email_log).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    template=template,
                    category="transactional",
                    recipient_hash=rhash,
                    status=status,
                    provider=self.provider.name,
                    provider_message_id=provider_message_id,
                    error_code=error_code[:64] if error_code else None,
                    requested_by=requested_by[:128],
                )
            )

    async def send(
        self,
        db: Database,
        redis: Redis | None,
        *,
        template: str,
        to: str,
        variables: dict[str, str],
        tenant_id: uuid.UUID | None,
        requested_by: str,
    ) -> str:
        """Returns the provider message id. Raises a problem error (422/429/503) on failure."""
        address = normalize_address(to)
        rhash = recipient_hash(address)
        rendered = render(template, variables, public_base_url=self._base)
        if not await allow_fixed_window(redis, f"notify:mail:rcpt:{rhash}", self._limit, 3600):
            await self._log(db, tenant_id=tenant_id, template=template, rhash=rhash, status="rate_limited",
                            requested_by=requested_by)  # fmt: skip
            raise TooManyRequests("Too many emails to this recipient; try again later")
        message = OutboundEmail(
            from_address=self._from_address,
            from_name=self._from_name,
            to_address=address,
            subject=rendered.subject,
            text=rendered.text,
            html=rendered.html,
            category="transactional",
            idempotency_key=str(uuid.uuid4()),
        )
        try:
            creds = await self._credentials.get()
            result = await self.provider.send(creds, message)
        except PermanentSendError as exc:
            await self._log(db, tenant_id=tenant_id, template=template, rhash=rhash, status="rejected",
                            requested_by=requested_by, error_code=str(exc) or "rejected")  # fmt: skip
            log.warning("email_rejected", extra={"template": template, "error_code": str(exc)})
            raise EmailRejected("The email provider rejected the message") from exc
        except TransientSendError as exc:
            await self._log(db, tenant_id=tenant_id, template=template, rhash=rhash, status="failed",
                            requested_by=requested_by, error_code=str(exc) or "transient")  # fmt: skip
            log.warning("email_send_failed", extra={"template": template, "error_code": str(exc)})
            raise ServiceUnavailable("Email delivery is temporarily unavailable") from exc
        await self._log(db, tenant_id=tenant_id, template=template, rhash=rhash, status="sent",
                        requested_by=requested_by, provider_message_id=result.provider_message_id)  # fmt: skip
        return result.provider_message_id
