"""Tenant provider credentials, resolved at execution time (build.md §19.2).

Flow (real mode): integration-service returns the *reference* for the tenant's active connection →
this service reads the secret from Vault under ITS OWN identity/policy → values stay in memory only.
Queue payloads, events, logs and API responses carry `connection_id` at most, never secret values.

Fake mode (development/test only): no Vault lookup; adapters talk to the fake-providers container and
use the shared development-only `FAKE_PROVIDER_SECRET` for webhook signatures.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from pydantic import SecretStr

from nexus_core.clients import ServiceClient, UpstreamError
from nexus_core.config import ProviderMode, ServiceSettings
from nexus_core.context import Actor
from nexus_core.errors import ProblemError
from nexus_core.vault import VaultClient, VaultNotFound, platform_secret_path

CACHE_SECONDS = 60


class ProviderNotConfigured(ProblemError):
    status, code, title = 409, "provider_not_configured", "Provider connection is not configured"


@dataclass(frozen=True, slots=True)
class ResolvedCredentials:
    provider: str
    connection_id: str | None  # None when the platform default or the development fake is used
    values: dict[str, SecretStr]
    config: dict[str, str]
    source: str  # tenant | platform | fake

    def secret(self, name: str) -> str:
        if name not in self.values:
            raise ProviderNotConfigured(f"Provider credential field '{name}' is missing")
        return self.values[name].get_secret_value()


class CredentialResolver:
    def __init__(self, settings: ServiceSettings, vault: VaultClient | None, integration: ServiceClient | None) -> None:
        self._settings = settings
        self._vault = vault
        self._integration = integration
        self._cache: dict[tuple[str, str], tuple[float, ResolvedCredentials]] = {}

    def invalidate(self, tenant_id: uuid.UUID | str, provider: str) -> None:
        self._cache.pop((str(tenant_id), provider), None)

    async def resolve(
        self,
        tenant_id: uuid.UUID | str,
        provider: str,
        *,
        actor: Actor | None = None,
        allow_platform_default: bool = False,
    ) -> ResolvedCredentials:
        if self._settings.provider_mode is ProviderMode.FAKE:
            secret = self._settings.fake_provider_secret or SecretStr("fake-provider-secret-dev-only")
            return ResolvedCredentials(provider, None, {"fake_secret": secret}, {}, "fake")
        key = (str(tenant_id), provider)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if self._vault is None:
            raise ProviderNotConfigured("Secrets backend is not configured")
        resolved: ResolvedCredentials | None = None
        if self._integration is not None:
            try:
                ref = await self._integration.get_json(
                    "/internal/v1/connections/resolve", params={"provider": provider}, tenant_id=tenant_id, actor=actor
                )
            except UpstreamError as exc:
                if not exc.is_not_found:
                    raise
                ref = None
            if ref and ref.get("status") == "active":
                values = await self._vault.read(ref["vault_ref"])
                resolved = ResolvedCredentials(
                    provider,
                    str(ref["connection_id"]),
                    values,
                    {k: str(v) for k, v in (ref.get("config") or {}).items()},
                    "tenant",
                )
        if resolved is None and allow_platform_default:
            path = platform_secret_path(self._settings.env.value, f"{self._settings.service_name}/{provider}")
            try:
                resolved = ResolvedCredentials(provider, None, await self._vault.read(path), {}, "platform")
            except VaultNotFound:
                resolved = None
        if resolved is None:
            raise ProviderNotConfigured(f"No active {provider} connection for this tenant")
        self._cache[key] = (time.monotonic() + CACHE_SECONDS, resolved)
        return resolved
