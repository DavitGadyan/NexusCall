"""Minimal async HashiCorp Vault client (ADR-0009).

Supports AppRole login (local/dev), a pre-issued token file (Vault Agent sink in
Kubernetes) and KV v2 read/write/delete/metadata. Secret values are returned as
`SecretStr` and never logged; errors are reported without response bodies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

log = logging.getLogger(__name__)


class VaultError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class VaultPermissionDenied(VaultError):
    pass


class VaultNotFound(VaultError):
    pass


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    path: str
    current_version: int | None
    created_time: str | None
    updated_time: str | None


class VaultClient:
    def __init__(
        self,
        addr: str,
        *,
        kv_mount: str = "secret",
        role_id_file: str | None = None,
        secret_id_file: str | None = None,
        token_file: str | None = None,
        token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._mount = kv_mount
        self._role_id_file = role_id_file
        self._secret_id_file = secret_id_file
        self._token_file = token_file
        self._token: str | None = token
        self._token_expires = float("inf") if token else 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=timeout)
        self.last_success_at: float | None = None
        self.last_error: str | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        if self._token_file and Path(self._token_file).exists():
            self._token = Path(self._token_file).read_text().strip()
            self._token_expires = time.monotonic() + 60  # re-read the sink regularly
            return
        if not (self._role_id_file and self._secret_id_file):
            raise VaultError("No Vault authentication method configured")
        role_id = Path(self._role_id_file).read_text().strip()
        secret_id = Path(self._secret_id_file).read_text().strip()
        resp = await self._http.post(
            f"{self._addr}/v1/auth/approle/login", json={"role_id": role_id, "secret_id": secret_id}
        )
        if resp.status_code != 200:
            raise VaultError("Vault AppRole login failed", resp.status_code)
        auth = resp.json()["auth"]
        self._token = auth["client_token"]
        self._token_expires = time.monotonic() + float(auth.get("lease_duration", 300))

    async def _headers(self) -> dict[str, str]:
        async with self._lock:
            if not self._token or time.monotonic() > self._token_expires - 30:
                await self._login()
        return {"X-Vault-Token": self._token or ""}

    async def _request(self, method: str, path: str, json: Any = None) -> dict[str, Any]:
        resp = await self._http.request(method, f"{self._addr}/v1/{path}", json=json, headers=await self._headers())
        if resp.status_code in (200, 204):
            self.last_success_at = time.time()
            self.last_error = None
            return resp.json() if resp.content else {}
        self.last_error = f"http_{resp.status_code}"
        if resp.status_code == 403:
            raise VaultPermissionDenied("Vault denied access to the requested path", 403)
        if resp.status_code == 404:
            raise VaultNotFound("Secret not found", 404)
        raise VaultError("Vault request failed", resp.status_code)

    async def read(self, path: str) -> dict[str, SecretStr]:
        data = await self._request("GET", f"{self._mount}/data/{path}")
        return {k: SecretStr(str(v)) for k, v in data["data"]["data"].items()}

    async def write(self, path: str, values: dict[str, str]) -> int:
        data = await self._request("POST", f"{self._mount}/data/{path}", json={"data": values})
        return int(data.get("data", {}).get("version", 0))

    async def metadata(self, path: str) -> SecretMetadata:
        data = (await self._request("GET", f"{self._mount}/metadata/{path}"))["data"]
        return SecretMetadata(path, data.get("current_version"), data.get("created_time"), data.get("updated_time"))

    async def destroy(self, path: str) -> None:
        await self._request("DELETE", f"{self._mount}/metadata/{path}")

    async def health(self) -> bool:
        resp = await self._http.get(f"{self._addr}/v1/sys/health")
        return resp.status_code in (200, 429, 473)


def tenant_secret_path(env: str, tenant_id: str, provider: str, connection_id: str) -> str:
    for part in (env, provider):
        if not part.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Invalid Vault path component")
    return f"{env}/tenants/{tenant_id}/{provider}/{connection_id}"


def platform_secret_path(env: str, name: str) -> str:
    if not name.replace("_", "").replace("-", "").replace("/", "").isalnum():
        raise ValueError("Invalid Vault path component")
    return f"{env}/platform/{name}"
