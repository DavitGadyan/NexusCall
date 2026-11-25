"""Service settings loaded from environment variables (never hardcoded; build.md §5)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ProviderMode(StrEnum):
    """`fake` selects deterministic local simulators; only legal in development/test."""

    FAKE = "fake"
    REAL = "real"


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", case_sensitive=False)

    service_name: str = Field(default="unknown-service")
    env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"

    # Data plane. The URL must use the service's *_app role (non-owner, no BYPASSRLS).
    database_url: SecretStr = SecretStr("postgresql+asyncpg://localhost/unset")
    # Read-only catalog role for the RLS coverage dashboard; audited cross-tenant role (break-glass/platform audit).
    inspector_database_url: SecretStr | None = None
    platform_database_url: SecretStr | None = None
    rls_scan_clients: tuple[str, ...] = ("svc-tenant-service",)
    db_pool_size: int = 10
    db_max_overflow: int = 5
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    amqp_url: SecretStr = SecretStr("amqp://guest:guest@localhost:5672/nexus")

    # Identity
    oidc_issuer: str = "http://localhost:8080/realms/nexus"
    oidc_jwks_url: str | None = None
    # Back-channel token endpoint (internal network). Defaults to the issuer's endpoint.
    oidc_token_url: str | None = None
    oidc_audience: str = "nexus-api"
    oidc_allowed_algs: tuple[str, ...] = ("RS256",)
    oidc_leeway_seconds: int = 30
    # Allowed `azp` (client ids) for /internal endpoints.
    internal_client_allowlist: tuple[str, ...] = ()
    # This service's own client-credentials identity (secret delivered by Vault Agent / dev env).
    service_client_id: str | None = None
    service_client_secret: SecretStr | None = None
    service_client_secret_file: str | None = None

    # Service discovery (internal network)
    tenant_service_url: str = "http://tenant-service:8000"
    audit_service_url: str = "http://audit-service:8000"

    # Secrets
    vault_addr: str = "http://vault:8200"
    vault_role_id_file: str | None = None
    vault_secret_id_file: str | None = None
    vault_token_file: str | None = None
    vault_kv_mount: str = "secret"

    # Providers
    provider_mode: ProviderMode = ProviderMode.REAL
    fake_providers_url: str = "http://fake-providers:8000"

    # Observability
    otel_exporter_otlp_endpoint: str | None = None
    sentry_dsn: SecretStr | None = None

    # Background loops inside the service process
    outbox_relay_enabled: bool = True
    event_consumers_enabled: bool = True

    # HTTP limits
    max_request_body_bytes: int = 1_048_576
    authz_cache_ttl_seconds: int = 30

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"

    @property
    def token_url(self) -> str:
        return self.oidc_token_url or f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/token"

    def client_secret_value(self) -> str | None:
        if self.service_client_secret_file:
            from pathlib import Path

            path = Path(self.service_client_secret_file)
            if path.exists():
                return path.read_text().strip()
        return self.service_client_secret.get_secret_value() if self.service_client_secret else None

    @property
    def is_production_like(self) -> bool:
        return self.env in (Environment.STAGING, Environment.PRODUCTION)

    @model_validator(mode="after")
    def _forbid_fakes_outside_dev(self) -> Self:
        # build.md §9.3/§19.5: fakes must be impossible to enable accidentally in production.
        if self.provider_mode is ProviderMode.FAKE and self.is_production_like:
            raise ValueError(f"PROVIDER_MODE=fake is forbidden when ENV={self.env.value}; configure real providers.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> ServiceSettings:
    return ServiceSettings()
