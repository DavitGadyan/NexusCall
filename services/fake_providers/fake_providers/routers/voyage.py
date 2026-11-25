"""DEVELOPMENT/TEST ONLY: deterministic Voyage AI embeddings simulator (`POST /voyage/v1/embeddings`).

Speaks the Voyage wire shape the gateway's real `VoyageEmbeddings` adapter parses, so pointing
`VOYAGE_BASE_URL` at `http://fake-providers:8000/voyage` exercises the real adapter code path. Vectors are
hash-based unit vectors: texts that share words get similar vectors, identical texts get identical ones."""

from __future__ import annotations

import hashlib
import math
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/voyage", tags=["fake-voyage"])
MAX_INPUTS = 128
MAX_CHARS = 32_000


class EmbeddingsIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1, max_length=80)
    input: list[str] | str
    output_dimension: int = Field(default=1024, ge=64, le=4096)
    input_type: str | None = None


def unit_vector(text: str, dims: int) -> list[float]:
    vec = [0.0] * dims
    for token in text.lower().split():
        h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
        vec[h % dims] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@router.post("/v1/embeddings")
async def embeddings(body: EmbeddingsIn, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer ") or len(authorization) < 10:
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts or len(texts) > MAX_INPUTS or any(len(t) > MAX_CHARS for t in texts):
        raise HTTPException(status_code=400, detail="invalid input size")
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": unit_vector(t, body.output_dimension), "index": i}
            for i, t in enumerate(texts)
        ],
        "model": body.model,
        "usage": {"total_tokens": sum(len(t.split()) for t in texts)},
    }
