"""Request/response models. Credential inputs are `SecretStr`; no response model has a field that could carry
a secret value."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=2, max_length=32, pattern=r"^[a-z_]+$")
    scope: str = Field("default", min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    display_name: str | None = Field(default=None, max_length=120)
    credentials: dict[str, SecretStr] = Field(min_length=1, max_length=10)
    config: dict[str, str] = Field(default_factory=dict, max_length=10)


class PlatformConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=2, max_length=32, pattern=r"^[a-z_]+$")
    owning_service: str = Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9-]+$")
    display_name: str | None = Field(default=None, max_length=120)
    credentials: dict[str, SecretStr] = Field(min_length=1, max_length=10)
    config: dict[str, str] = Field(default_factory=dict, max_length=10)


class RotateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credentials: dict[str, SecretStr] = Field(min_length=1, max_length=10)
    reason: Literal["planned", "suspected_compromise", "provider_requested", "other"] = "planned"


class ConnectionOut(BaseModel):
    id: uuid.UUID
    provider: str
    scope: str
    owning_service: str
    display_name: str | None
    status: str
    auth_type: str
    vault_ref: str
    vault_version: int | None
    account_masked: str | None
    granted_scopes: list[str]
    config: dict[str, str]
    last_error: str | None
    last_validated_at: datetime | None
    last_rotated_at: datetime | None
    next_rotation_due: datetime | None
    last_vault_status: str | None
    last_resolved_at: datetime | None
    health: str
    version: int
    created_at: datetime
    updated_at: datetime


class ConnectionPage(BaseModel):
    items: list[ConnectionOut]
    next_cursor: str | None


class ProviderOut(BaseModel):
    provider: str
    credential_fields: list[str]
    optional_fields: list[str]
    config_keys: list[str]
    oauth: bool
    platform_only: bool
    platform_services: list[str]


class ProviderList(BaseModel):
    items: list[ProviderOut]


class OAuthStartOut(BaseModel):
    authorization_url: str
    expires_at: datetime


class ResolveOut(BaseModel):
    connection_id: uuid.UUID
    provider: str
    status: str
    vault_ref: str
    config: dict[str, str]


class SecretRefOut(BaseModel):
    vault_ref: str
    scope: str
    tenant_id: uuid.UUID | None
    connection_id: uuid.UUID | None
    owning_service: str
    provider: str
    kind: str
    environment: str
    status: str
    vault_version: int | None
    last_rotated_at: datetime | None
    next_rotation_due: datetime | None
    last_retrieval_at: datetime | None
    last_vault_status: str | None
    last_checked_at: datetime | None
    health: str


class SecretRefPage(BaseModel):
    items: list[SecretRefOut]
    next_cursor: str | None


class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=10, max_length=2048)
    event_types: list[str] = Field(min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=200)
    # Supplied by the tenant (generated on their side) so the API never has to return a secret.
    secret: SecretStr = Field(min_length=32, max_length=256)


class EndpointPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, min_length=10, max_length=2048)
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=200)
    status: Literal["active", "disabled"] | None = None
    expected_version: int


class SecretRotateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: SecretStr = Field(min_length=32, max_length=256)


class EndpointOut(BaseModel):
    id: uuid.UUID
    url: str
    event_types: list[str]
    description: str | None
    status: str
    secret_ref: str
    secret_version: int | None
    consecutive_failures: int
    last_delivery_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class EndpointPage(BaseModel):
    items: list[EndpointOut]
    next_cursor: str | None


class DeliveryOut(BaseModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime


class DeliveryPage(BaseModel):
    items: list[DeliveryOut]
    next_cursor: str | None


class AttemptOut(BaseModel):
    attempt: int
    outcome: str
    status_code: int | None
    error: str | None
    duration_ms: int | None
    created_at: datetime


class DeliveryDetail(DeliveryOut):
    attempt_log: list[AttemptOut]
