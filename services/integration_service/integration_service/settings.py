from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "integration-service"
    # Every service may resolve its tenant's connection reference (BUILD_CONVENTIONS §5, integration-service).
    internal_client_allowlist: tuple[str, ...] = (
        "svc-call-service",
        "svc-voice-service",
        "svc-ai-gateway",
        "svc-ai-agent-service",
        "svc-tool-execution",
        "svc-booking-service",
        "svc-email-service",
        "svc-email-inbox-service",
        "svc-crm-service",
        "svc-notification-service",
    )

    # Key for HMAC-ing secret material where a stable fingerprint is unavoidable (idempotency request hashes,
    # PKCE verifier derivation). Delivered by Vault Agent / env; never stored or logged.
    integration_hmac_key: SecretStr | None = None
    integration_hmac_key_file: str | None = None

    # OAuth (authorization code). The redirect URI must be registered with each provider.
    oauth_redirect_path: str = "/oauth/callback"
    oauth_state_ttl_seconds: int = 600
    oauth_success_path: str = "/integrations"
    # Browsers reach the fake-providers simulator through this URL in development.
    fake_providers_public_url: str | None = None

    # Rotation policy for static credentials (days) and reminder lead time.
    rotation_period_days: int = 90
    rotation_reminder_days: int = 14

    # Outbound webhooks
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 6
    webhook_backoff_base_seconds: float = 30.0
    webhook_backoff_max_seconds: float = 3600.0
    webhook_max_endpoints_per_tenant: int = 20
    webhook_sweep_batch: int = 100

    # Inbound HubSpot webhooks: replay window for X-HubSpot-Request-Timestamp (ms).
    hubspot_webhook_tolerance_seconds: int = 300

    # Provider connection tests
    provider_test_timeout_seconds: float = 8.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
