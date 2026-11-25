from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel

ProviderCallStatus = Literal[
    "queued", "initiated", "ringing", "in-progress", "completed", "busy", "failed", "no-answer", "canceled", "unknown"
]


class CreateCallRequest(BaseModel):
    to: str
    from_: str
    status_callback_url: str
    answer_url: str
    idempotency_key: str
    timeout_seconds: int = 30
    record: bool = False
    machine_detection: bool = True


class ProviderCall(BaseModel):
    provider_call_id: str
    status: ProviderCallStatus
    duration_seconds: int | None = None
    price_micros: int | None = None


class TelephonyCredentials(BaseModel):
    account_sid: str
    auth_token: str


class AmbiguousProviderResult(Exception):
    """Timeout/5xx where the provider may or may not have acted: reconcile before retrying."""


class ProviderRejected(Exception):
    """Permanent rejection (invalid number, auth failure). Never retried."""


class TelephonyProvider(Protocol):
    name: str

    async def create_call(self, creds: TelephonyCredentials, req: CreateCallRequest) -> ProviderCall: ...

    async def get_call(self, creds: TelephonyCredentials, provider_call_id: str) -> ProviderCall: ...

    async def find_call_by_idempotency_key(self, creds: TelephonyCredentials, key: str) -> ProviderCall | None: ...

    async def hangup(self, creds: TelephonyCredentials, provider_call_id: str) -> None: ...

    async def transfer_to_conference(
        self, creds: TelephonyCredentials, provider_call_id: str, conference_name: str
    ) -> None: ...
