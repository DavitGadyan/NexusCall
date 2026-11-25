"""Contract test of the Voyage simulator against the wire shape the gateway's Voyage adapter expects."""

from __future__ import annotations

import math

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_providers.routers.voyage import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_voyage_fake_returns_deterministic_unit_vectors_in_wire_shape():
    client = _client()
    body = {
        "model": "voyage-3",
        "input": ["opening hours", "opening hours", "drain cleaning"],
        "output_dimension": 1024,
    }
    resp = client.post("/voyage/v1/embeddings", json=body, headers={"Authorization": "Bearer fake-key-123"})
    assert resp.status_code == 200
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    assert [d["index"] for d in data] == [0, 1, 2] and resp.json()["model"] == "voyage-3"
    vectors = [d["embedding"] for d in data]
    assert all(len(v) == 1024 and math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-6) for v in vectors)
    assert vectors[0] == vectors[1] and vectors[0] != vectors[2]


def test_voyage_fake_requires_a_key_and_bounded_input():
    client = _client()
    assert client.post("/voyage/v1/embeddings", json={"model": "voyage-3", "input": ["x"]}).status_code == 401
    too_many = {"model": "voyage-3", "input": ["x"] * 129}
    assert (
        client.post("/voyage/v1/embeddings", json=too_many, headers={"Authorization": "Bearer k-123456"}).status_code
        == 400
    )
