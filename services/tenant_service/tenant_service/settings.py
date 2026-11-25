from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "tenant-service"
    public_base_url: str = "http://localhost:47000"
    notification_service_url: str = "http://notification-service:8000"
    keycloak_admin_base: str = "http://keycloak:8080"
    keycloak_realm: str = "nexus"
    bff_client_id: str = "web-bff"
    idp_mode: str = "keycloak"  # keycloak | fake (fake only in development/test)
    # Broker operations (queue view, dead-letter replay). Credentials come from Vault at
    # platform/tenant-service/rabbitmq-ops (fields: username, password); never from code.
    rabbitmq_management_url: str = "http://rabbitmq:15672/api"
    rabbitmq_amqp_host: str = "rabbitmq"
    rabbitmq_amqp_port: int = 5672
    rabbitmq_vhost: str = "nexus"
    infra_health_targets: tuple[str, ...] = ("web",)
    internal_client_allowlist: tuple[str, ...] = (
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
        "svc-notification-service",
        "svc-analytics-service",
        "svc-audit-service",
    )
    rls_scan_targets: tuple[str, ...] = (
        "tenant-service",
        "crm-service",
        "campaign-service",
        "call-service",
        "voice-service",
        "ai-agent-service",
        "ai-gateway",
        "mcp-registry",
        "tool-execution",
        "booking-service",
        "email-service",
        "email-inbox-service",
        "integration-service",
        "notification-service",
        "analytics-service",
        "audit-service",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
