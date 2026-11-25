import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Column, DateTime, MetaData, Table, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

from nexus_core.circuit import CircuitBreaker, CircuitOpen
from nexus_core.errors import Forbidden, Unprocessable
from nexus_core.pagination import apply_keyset, build_page, decode_cursor, encode_cursor
from nexus_core.rls import append_only_rls_statements
from nexus_core.storage import InMemoryObjectStore, assert_tenant_key, tenant_key

T = uuid.uuid4()


def test_tenant_key_is_prefixed_and_safe():
    key = tenant_key(T, "recordings", "abc.wav")
    assert key == f"{T}/recordings/abc.wav"
    assert assert_tenant_key(T, key) == key
    with pytest.raises(Forbidden):
        assert_tenant_key(uuid.uuid4(), key)
    for bad in ("../x", "a/b", "", ".."):
        with pytest.raises(ValueError):
            tenant_key(T, bad)
    with pytest.raises(Forbidden):
        assert_tenant_key(T, f"{T}/../other/secret")


async def test_in_memory_store_roundtrip_and_presign_cap():
    store = InMemoryObjectStore()
    key = tenant_key(T, "a.txt")
    await store.put("attachments", key, b"hello", content_type="text/plain")
    assert await store.get("attachments", key) == b"hello"
    assert "expires=900" in await store.presign_get("attachments", key, expires_seconds=99999)
    with pytest.raises(ValueError):
        await store.get("attachments", key, max_bytes=2)
    with pytest.raises(ValueError):
        await store.put("not-a-bucket", key, b"x")


def test_cursor_roundtrip_and_keyset_sql():
    now = datetime.now(UTC)
    rid = uuid.uuid4()
    cur = encode_cursor(now, rid)
    assert decode_cursor(cur) == (now, str(rid))
    with pytest.raises(Unprocessable):
        decode_cursor("not-a-cursor")
    t = Table("things", MetaData(), Column("id", UUID(as_uuid=True)), Column("created_at", DateTime(timezone=True)))
    sql = str(apply_keyset(select(t), t.c.created_at, t.c.id, cur, 10).compile(dialect=postgresql.dialect()))
    assert "ORDER BY things.created_at DESC, things.id DESC" in sql and "LIMIT" in sql
    with pytest.raises(Unprocessable):
        apply_keyset(select(t), t.c.created_at, t.c.id, encode_cursor(now, "not-a-uuid"), 10)


def test_build_page_detects_more():
    rows = [{"id": uuid.uuid4(), "created_at": datetime.now(UTC) - timedelta(seconds=i)} for i in range(4)]
    page, cursor = build_page(rows, 3)
    assert len(page) == 3 and cursor is not None
    assert decode_cursor(cursor)[1] == str(rows[2]["id"])
    assert build_page(rows[:2], 3) == (rows[:2], None)


async def test_circuit_breaker_local_mode():
    cb = CircuitBreaker(None, "provider-x", failure_threshold=2, open_seconds=30)
    await cb.check()
    assert await cb.record_failure() is False
    assert await cb.record_failure() is True
    assert await cb.state() == "open"
    with pytest.raises(CircuitOpen):
        await cb.check()
    cb._local_open_until = 1.0  # elapsed -> half-open probe allowed
    assert await cb.state() == "half_open"
    await cb.check()
    assert await cb.record_failure() is True  # failed probe re-opens immediately
    await cb.record_success()
    assert await cb.state() == "closed"


def test_append_only_rls_has_no_update_or_delete_policy():
    sql = " ".join(append_only_rls_statements("audit_events", "audit_app", allow_null_tenant=True))
    assert "FOR SELECT" in sql and "FOR INSERT" in sql
    assert "FOR UPDATE" not in sql and "FOR DELETE" not in sql
    assert "BEFORE UPDATE OR DELETE" in sql and "BEFORE TRUNCATE" in sql
    assert "IS NOT DISTINCT FROM" in sql and "GRANT SELECT, INSERT ON audit_events TO audit_app" in sql
