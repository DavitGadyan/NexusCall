"""Hash-chained, append-only audit records (build.md §3.1P).

* Appends are serialized per chain with `pg_advisory_xact_lock`, so `chain_seq` is gap-free and
  `prev_hash` always points at the current head, even with several consumers running.
* `hash = sha256(prev_hash || canonical_json(fields))`. Canonical JSON uses sorted keys, no whitespace
  and UTF-8, so the verifier can recompute it from stored columns alone.
* Metadata is filtered: keys in `nexus_core.logging.SENSITIVE_KEYS` are dropped (at every depth), string
  values are redacted (emails, phone numbers, tokens) and truncated, and the total size is capped.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from audit_service.tables import PLATFORM_CHAIN, audit_events
from nexus_core.logging import SENSITIVE_KEYS, redact_text

GENESIS_HASH = "0" * 64
MAX_METADATA_BYTES = 4096
MAX_METADATA_DEPTH = 4
MAX_METADATA_KEYS = 50
MAX_STRING_CHARS = 256
TRUNCATED = {"_truncated": True}

# Fields covered by the hash, in a fixed set (order does not matter: canonical JSON sorts keys).
HASHED_FIELDS = (
    "event_id",
    "tenant_id",
    "chain",
    "chain_seq",
    "occurred_at",
    "actor_type",
    "actor_id",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "request_id",
    "correlation_id",
    "causation_id",
    "producer",
    "metadata",
)


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return k in SENSITIVE_KEYS or any(s in k for s in ("password", "secret", "token", "api_key", "authorization"))


def _clean(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return redact_text(value)[:MAX_STRING_CHARS]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if depth >= MAX_METADATA_DEPTH:
        return "[DEPTH]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:MAX_METADATA_KEYS]:
            key = str(k)[:64]
            if _is_sensitive(key):
                continue
            out[key] = _clean(v, depth + 1)
        return out
    if isinstance(value, list | tuple | set):
        return [_clean(v, depth + 1) for v in list(value)[:MAX_METADATA_KEYS]]
    return redact_text(str(value))[:MAX_STRING_CHARS]


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Safe metadata only: no sensitive keys, redacted strings, bounded depth, keys and size."""
    if not metadata:
        return {}
    cleaned = _clean(dict(metadata), 0)
    assert isinstance(cleaned, dict)
    if len(canonical_json(cleaned).encode()) > MAX_METADATA_BYTES:
        return dict(TRUNCATED)
    return cleaned


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _norm(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "occurred_at":
        assert isinstance(value, datetime)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def compute_hash(prev_hash: str, fields: Mapping[str, Any]) -> str:
    material = {name: _norm(name, fields.get(name)) for name in HASHED_FIELDS}
    return hashlib.sha256(prev_hash.encode() + canonical_json(material).encode()).hexdigest()


def chain_for(tenant_id: uuid.UUID | None) -> str:
    return str(tenant_id) if tenant_id is not None else PLATFORM_CHAIN


def _lock_key(chain: str) -> int:
    # Stable signed 64-bit key per chain (Python's hash() is salted per process, so never use it here).
    return int.from_bytes(hashlib.sha256(f"audit-chain:{chain}".encode()).digest()[:8], "big", signed=True)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    event_id: uuid.UUID
    tenant_id: uuid.UUID | None
    occurred_at: datetime
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: str | None
    correlation_id: uuid.UUID | None
    causation_id: uuid.UUID | None
    producer: str
    metadata: Mapping[str, Any]


async def append(session: AsyncSession, record: AuditRecord) -> uuid.UUID | None:
    """Append one record to its chain inside the caller's transaction.

    The session must carry the record's tenant context (or none for platform records); RLS enforces it.
    Returns the new row id, or None when the event id was already recorded (idempotent)."""
    chain = chain_for(record.tenant_id)
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(chain)})
    duplicate = (
        await session.execute(select(audit_events.c.id).where(audit_events.c.event_id == record.event_id))
    ).first()
    if duplicate is not None:
        return None
    head = (
        await session.execute(
            select(audit_events.c.chain_seq, audit_events.c.hash)
            .where(audit_events.c.chain == chain)
            .order_by(audit_events.c.chain_seq.desc())
            .limit(1)
        )
    ).first()
    seq, prev = (head.chain_seq + 1, head.hash) if head else (1, GENESIS_HASH)
    occurred = record.occurred_at if record.occurred_at.tzinfo else record.occurred_at.replace(tzinfo=UTC)
    # Round-trip through PostgreSQL keeps microseconds; normalize now so the stored value hashes the same.
    fields: dict[str, Any] = {
        "event_id": record.event_id,
        "tenant_id": record.tenant_id,
        "chain": chain,
        "chain_seq": seq,
        "occurred_at": occurred.astimezone(UTC),
        "actor_type": record.actor_type,
        "actor_id": record.actor_id[:128],
        "action": record.action[:128],
        "resource_type": record.resource_type[:64],
        "resource_id": record.resource_id[:128] if record.resource_id else None,
        "outcome": record.outcome,
        "request_id": record.request_id[:128] if record.request_id else None,
        "correlation_id": record.correlation_id,
        "causation_id": record.causation_id,
        "producer": record.producer[:64],
        "metadata": sanitize_metadata(record.metadata),
    }
    digest = compute_hash(prev, fields)
    row_id = (
        await session.execute(
            insert(audit_events)
            .values(**fields, prev_hash=prev, hash=digest)
            .on_conflict_do_nothing(constraint="uq_audit_events_event_id")
            .returning(audit_events.c.id)
        )
    ).scalar_one_or_none()
    return row_id


@dataclass(frozen=True, slots=True)
class BrokenLink:
    chain_seq: int
    row_id: str
    reason: str  # sequence_gap | prev_hash_mismatch | hash_mismatch


@dataclass(slots=True)
class ChainVerifier:
    """Incremental verifier; feed rows in `chain_seq` order (batches are fine)."""

    expected_seq: int = 1
    prev_hash: str = GENESIS_HASH
    checked: int = 0
    broken: BrokenLink | None = None

    def feed(self, rows: Iterable[Mapping[str, Any]]) -> bool:
        """Returns False once a broken link was found (stop feeding)."""
        for row in rows:
            if self.broken is not None:
                return False
            seq = int(row["chain_seq"])
            reason = None
            if seq != self.expected_seq:
                reason = "sequence_gap"
            elif row["prev_hash"] != self.prev_hash:
                reason = "prev_hash_mismatch"
            elif compute_hash(row["prev_hash"], row) != row["hash"]:
                reason = "hash_mismatch"
            if reason is not None:
                self.broken = BrokenLink(seq, str(row["id"]), reason)
                return False
            self.checked += 1
            self.expected_seq = seq + 1
            self.prev_hash = row["hash"]
        return True

    @property
    def ok(self) -> bool:
        return self.broken is None


def verify_rows(rows: Iterable[Mapping[str, Any]]) -> ChainVerifier:
    verifier = ChainVerifier()
    verifier.feed(rows)
    return verifier
