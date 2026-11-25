"""Client-credentials token acquisition for service-to-service calls (ADR-0006)."""

from __future__ import annotations

import asyncio
import time

import httpx


class ServiceTokenProvider:
    def __init__(self, token_url: str, client_id: str, client_secret: str, *, audience: str | None = None):
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._audience = audience
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - 30:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - 30:
                return self._token
            data = {"grant_type": "client_credentials"}
            if self._audience:
                data["audience"] = self._audience
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self._token_url, data=data, auth=(self._client_id, self._client_secret))
                resp.raise_for_status()
                body = resp.json()
            self._token = str(body["access_token"])
            self._expires_at = time.monotonic() + float(body.get("expires_in", 60))
            return self._token
