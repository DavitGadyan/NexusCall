from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "tool-execution"
    # The AI gateway is the only caller of the execute endpoint (BUILD_CONVENTIONS §5).
    gateway_clients: tuple[str, ...] = ("svc-ai-gateway",)
    approval_ttl_seconds: int = 1800
    max_argument_bytes: int = 16_384
    approval_expiry_interval_seconds: float = 60.0
    # MCP egress. Hosts approved at DEPLOYMENT time for internal MCP servers on private networks; a
    # server may reach one only if it is also on that server's own allowed_hosts list.
    mcp_private_hosts: tuple[str, ...] = ()
    mcp_allow_http_private: bool = False  # development/test only
    mcp_protocol_version: str = "2025-06-18"
    mcp_quarantine_seconds: int = 3600
    mcp_breaker_failures: int = 5
    mcp_breaker_open_seconds: int = 30


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
