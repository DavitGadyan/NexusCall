"""Embedding adapters: Voyage (real, httpx) and a deterministic fake (development/test only)."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import httpx

from ai_gateway.providers.base import ProviderPermanentError, ProviderTransientError


class VoyageEmbeddings:
    name = "voyage"

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def embed(self, api_key: str, model: str, texts: list[str], dims: int) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport, follow_redirects=False) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/embeddings",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "input": texts, "output_dimension": dims},
                )
        except httpx.HTTPError:
            raise ProviderTransientError("provider_unreachable") from None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderTransientError(f"provider_http_{resp.status_code}")
        if resp.status_code != 200:
            raise ProviderPermanentError(f"provider_http_{resp.status_code}")
        data: Any = resp.json().get("data", [])
        vectors = [list(map(float, item["embedding"])) for item in sorted(data, key=lambda d: d["index"])]
        if len(vectors) != len(texts) or any(len(v) != dims for v in vectors):
            raise ProviderPermanentError("provider_bad_embedding_shape")
        return vectors


class FakeEmbeddings:
    """Deterministic unit vectors from token hashes, so similar texts get similar vectors in tests."""

    name = "fake"

    async def embed(self, api_key: str, model: str, texts: list[str], dims: int) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * dims
            for token in text.lower().split():
                h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
                vec[h % dims] += 1.0 if (h >> 63) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out
