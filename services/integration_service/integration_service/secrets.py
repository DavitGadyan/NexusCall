"""The only code in NexusCall that writes tenant provider secrets to Vault (build.md §19.2).

Secret values exist as `SecretStr` in memory, are unwrapped only at the moment they are sent to Vault or to the
provider, and are never logged, persisted in PostgreSQL/Redis, put in events or returned by an API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as pysecrets
from pathlib import Path

from pydantic import SecretStr

from integration_service.settings import Settings
from nexus_core.errors import ServiceUnavailable
from nexus_core.vault import VaultClient, VaultError, VaultNotFound, VaultPermissionDenied

log = logging.getLogger(__name__)


class SecretsUnavailable(ServiceUnavailable):
    code, title = "secrets_unavailable", "Secrets backend unavailable"


def vault_status(exc: Exception) -> str:
    if isinstance(exc, VaultPermissionDenied):
        return "permission_denied"
    if isinstance(exc, VaultNotFound):
        return "not_found"
    status = getattr(exc, "status", None)
    return f"http_{status}" if status else "unreachable"


class SecretStore:
    """Thin wrapper over `VaultClient` that maps every failure to a value-free status code."""

    def __init__(self, vault: VaultClient | None) -> None:
        self.vault = vault

    def _client(self) -> VaultClient:
        if self.vault is None:
            raise SecretsUnavailable("Vault is not configured")
        return self.vault

    async def write(self, path: str, values: dict[str, SecretStr]) -> int:
        try:
            return await self._client().write(path, {k: v.get_secret_value() for k, v in values.items()})
        except (VaultError, OSError) as exc:
            log.warning("vault_write_failed", extra={"vault_status": vault_status(exc)})
            raise SecretsUnavailable("Could not store the credential in Vault") from exc

    async def read(self, path: str) -> dict[str, SecretStr]:
        try:
            return await self._client().read(path)
        except (VaultError, OSError) as exc:
            log.warning("vault_read_failed", extra={"vault_status": vault_status(exc)})
            raise SecretsUnavailable("Could not read the credential from Vault") from exc

    async def destroy(self, path: str) -> None:
        """Deletes every version and the metadata (KV v2 `DELETE metadata/...`). Missing paths are fine."""
        try:
            await self._client().destroy(path)
        except VaultNotFound:
            return
        except (VaultError, OSError) as exc:
            log.warning("vault_destroy_failed", extra={"vault_status": vault_status(exc)})
            raise SecretsUnavailable("Could not delete the credential from Vault") from exc

    async def health(self, path: str) -> tuple[str, int | None]:
        try:
            meta = await self._client().metadata(path)
        except (VaultError, OSError) as exc:
            return vault_status(exc), None
        return "ok", meta.current_version


def hmac_key(settings: Settings) -> bytes:
    """Server key for fingerprints of secret material. Required outside development/test."""
    if settings.integration_hmac_key_file and Path(settings.integration_hmac_key_file).exists():
        return Path(settings.integration_hmac_key_file).read_text().strip().encode()
    if settings.integration_hmac_key is not None:
        return settings.integration_hmac_key.get_secret_value().encode()
    if settings.is_production_like:
        raise RuntimeError("INTEGRATION_HMAC_KEY must be configured outside development/test")
    log.warning("integration_hmac_key_ephemeral")
    return pysecrets.token_bytes(32)


def fingerprint(key: bytes, values: dict[str, SecretStr]) -> str:
    """Keyed digest used only to compare idempotent requests; never reversible without the server key."""
    blob = json.dumps({k: v.get_secret_value() for k, v in sorted(values.items())}, separators=(",", ":"))
    return hmac.new(key, blob.encode(), hashlib.sha256).hexdigest()
