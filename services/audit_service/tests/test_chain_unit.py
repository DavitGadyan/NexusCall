"""Pure unit tests for metadata filtering, canonical hashing and the chain verifier (no Docker)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from audit_service.chain import (
    GENESIS_HASH,
    MAX_METADATA_BYTES,
    TRUNCATED,
    compute_hash,
    sanitize_metadata,
    verify_rows,
)


def test_sensitive_keys_are_dropped_at_every_depth_and_values_redacted():
    meta = sanitize_metadata(
        {
            "password": "hunter2",
            "Token": "abc",
            "client_secret": "s",
            "role": "analyst",
            "nested": {"access_token": "x", "email": "a@b.test", "note": "mail me at bob@example.test"},
            "items": [{"api_key": "k", "id": 1}],
            "x_auth_token_hint": "nope",
        }
    )
    assert meta == {
        "role": "analyst",
        "nested": {"note": "mail me at [EMAIL]"},
        "items": [{"id": 1}],
    }


def test_metadata_size_depth_and_non_finite_numbers_are_bounded():
    assert sanitize_metadata({"blob": ["x" * 200] * 49}) == TRUNCATED
    deep = sanitize_metadata({"a": {"b": {"c": {"d": {"e": 1}}}}})
    assert deep == {"a": {"b": {"c": {"d": "[DEPTH]"}}}}
    assert sanitize_metadata({"ratio": float("nan"), "n": 3}) == {"ratio": None, "n": 3}
    assert len(sanitize_metadata({"s": "y" * 10_000})["s"]) == 256
    assert MAX_METADATA_BYTES == 4096


def _fields(seq: int, **over):
    base = {
        "event_id": uuid.UUID(int=seq),
        "tenant_id": uuid.UUID(int=99),
        "chain": str(uuid.UUID(int=99)),
        "chain_seq": seq,
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=seq),
        "actor_type": "user",
        "actor_id": "u1",
        "action": "lead.exported",
        "resource_type": "lead",
        "resource_id": None,
        "outcome": "success",
        "request_id": None,
        "correlation_id": None,
        "causation_id": None,
        "producer": "crm-service",
        "metadata": {"count": seq},
    }
    base.update(over)
    return base


def _chain(n: int) -> list[dict]:
    rows, prev = [], GENESIS_HASH
    for seq in range(1, n + 1):
        f = _fields(seq)
        h = compute_hash(prev, f)
        rows.append({**f, "id": uuid.UUID(int=1000 + seq), "prev_hash": prev, "hash": h})
        prev = h
    return rows


def test_hash_is_canonical_and_covers_every_field():
    f = _fields(1)
    assert compute_hash(GENESIS_HASH, f) == compute_hash(GENESIS_HASH, dict(reversed(list(f.items()))))
    # Same instant in another timezone hashes the same; any content change does not.
    shifted = {**f, "occurred_at": f["occurred_at"].astimezone(datetime.now().astimezone().tzinfo)}
    assert compute_hash(GENESIS_HASH, shifted) == compute_hash(GENESIS_HASH, f)
    for key, value in (("action", "lead.deleted"), ("metadata", {"count": 2}), ("outcome", "denied")):
        assert compute_hash(GENESIS_HASH, {**f, key: value}) != compute_hash(GENESIS_HASH, f)
    assert compute_hash("1" * 64, f) != compute_hash(GENESIS_HASH, f)


def test_verifier_accepts_intact_chain_and_finds_first_broken_link():
    rows = _chain(5)
    ok = verify_rows(rows)
    assert ok.ok and ok.checked == 5

    tampered = [dict(r) for r in rows]
    tampered[2]["metadata"] = {"count": 999}  # content edited, hash left alone
    result = verify_rows(tampered)
    assert not result.ok and result.broken.chain_seq == 3 and result.broken.reason == "hash_mismatch"
    assert result.checked == 2

    rehashed = [dict(r) for r in rows]
    rehashed[1]["metadata"] = {"count": 7}  # attacker recomputes this row's hash but cannot fix the next link
    rehashed[1]["hash"] = compute_hash(rehashed[1]["prev_hash"], rehashed[1])
    result = verify_rows(rehashed)
    assert result.broken.chain_seq == 3 and result.broken.reason == "prev_hash_mismatch"

    gap = [r for r in rows if r["chain_seq"] != 4]  # a deleted row
    result = verify_rows(gap)
    assert result.broken.chain_seq == 5 and result.broken.reason == "sequence_gap"
