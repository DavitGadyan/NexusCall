from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "mcp-registry"
    # Only the AI gateway may list an agent's tools; only tool-execution may run a grant check.
    gateway_clients: tuple[str, ...] = ("svc-ai-gateway",)
    executor_clients: tuple[str, ...] = ("svc-tool-execution",)
    # Hosts explicitly approved at DEPLOYMENT time (env, not API) for internal MCP servers on private
    # networks. A server may use one only if it is also on that server's own allowed_hosts list.
    mcp_private_hosts: tuple[str, ...] = ()
    # Plain HTTP is accepted only for those private hosts and only in development/test.
    mcp_allow_http_private: bool = False
    policy_proposal_ttl_hours: int = 72


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
