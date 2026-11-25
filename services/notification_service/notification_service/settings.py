from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings

ALL_SERVICE_CLIENTS: tuple[str, ...] = (
    "svc-tenant-service",
    "svc-crm-service",
    "svc-campaign-service",
    "svc-call-service",
    "svc-voice-service",
    "svc-ai-agent-service",
    "svc-ai-gateway",
    "svc-mcp-registry",
    "svc-tool-execution",
    "svc-booking-service",
    "svc-email-service",
    "svc-email-inbox-service",
    "svc-integration-service",
    "svc-analytics-service",
    "svc-audit-service",
)


class Settings(ServiceSettings):
    service_name: str = "notification-service"
    # Callers of /internal/v1/notifications/* (service-account client ids, checked against `azp`).
    internal_client_allowlist: tuple[str, ...] = ALL_SERVICE_CLIENTS

    # Operational mail
    notification_from_address: str = "no-reply@nexus.local"
    notification_from_name: str = "NexusCall"
    ses_configuration_set: str | None = None
    mailpit_smtp_host: str = "mailpit:1025"  # fake mode only
    email_send_timeout_seconds: float = 10.0
    email_per_recipient_per_hour: int = 20

    # Server-sent events
    sse_heartbeat_seconds: float = 15.0
    sse_max_seconds: float = 900.0  # the client reconnects (and is re-authenticated) after this
    sse_authz_recheck_seconds: float = 60.0
    sse_max_streams_per_user: int = 5


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
