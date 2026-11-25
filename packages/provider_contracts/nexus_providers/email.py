from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel


class OutboundEmail(BaseModel):
    from_address: str
    from_name: str | None = None
    to_address: str
    reply_to: str | None = None
    subject: str
    html: str | None = None
    text: str
    headers: dict[str, str] = {}
    category: Literal["campaign", "transactional", "reply"]
    configuration_set: str | None = None
    idempotency_key: str


class SendResult(BaseModel):
    provider_message_id: str
    accepted: bool = True


class EmailCredentials(BaseModel):
    region: str
    access_key_id: str
    secret_access_key: str


class PermanentSendError(Exception):
    pass


class TransientSendError(Exception):
    pass


class EmailProvider(Protocol):
    name: str

    async def send(self, creds: EmailCredentials, message: OutboundEmail) -> SendResult: ...

    async def domain_status(self, creds: EmailCredentials, domain: str) -> dict[str, str]: ...
