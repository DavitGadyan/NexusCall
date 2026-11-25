"""Proves the tenant_session + RLS helpers isolate tenants under a restricted role,
including on a reused pooled connection. Requires Docker."""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.rls import COMMON_FUNCTIONS, inspect_coverage, tenant_rls_statements

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def pg():
    tc = pytest.importorskip("testcontainers.postgres")
    try:
        container = tc.PostgresContainer("postgres:16-alpine", driver=None)
        container.start()
    except Exception as exc:
        pytest.skip(f"Docker unavailable: {exc}")
    yield container
    container.stop()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def dbs(pg):
    host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
    admin = Database(f"postgresql+asyncpg://{pg.username}:{pg.password}@{host}:{port}/{pg.dbname}", pool_size=1)
    async with admin.engine.begin() as c:
        await c.execute(text("CREATE ROLE t_owner LOGIN PASSWORD 'o'"))
        await c.execute(text("CREATE ROLE t_app LOGIN PASSWORD 'a' NOSUPERUSER NOBYPASSRLS"))
        await c.execute(text(f"GRANT ALL ON DATABASE {pg.dbname} TO t_owner"))
        await c.execute(text("GRANT ALL ON SCHEMA public TO t_owner"))
        await c.execute(text("GRANT USAGE ON SCHEMA public TO t_app"))
    owner = Database(f"postgresql+asyncpg://t_owner:o@{host}:{port}/{pg.dbname}", pool_size=1)
    async with owner.engine.begin() as c:
        for stmt in COMMON_FUNCTIONS:
            await c.execute(text(stmt))
        await c.execute(text("CREATE TABLE notes (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, body text)"))
        for stmt in tenant_rls_statements("notes", "t_app"):
            await c.execute(text(stmt))
    app = Database(f"postgresql+asyncpg://t_app:a@{host}:{port}/{pg.dbname}", pool_size=1, max_overflow=0)
    yield owner, app
    await app.dispose()
    await owner.dispose()
    await admin.dispose()


A, B = uuid.uuid4(), uuid.uuid4()
ACTOR = Actor("user", "u")


async def test_tenant_isolation(dbs):
    owner, app = dbs
    async with app.tenant_session(A, ACTOR) as s:
        await s.execute(text("INSERT INTO notes VALUES (:i, :t, 'a-secret')"), {"i": uuid.uuid4(), "t": A})
    async with app.tenant_session(B, ACTOR) as s:
        await s.execute(text("INSERT INTO notes VALUES (:i, :t, 'b-secret')"), {"i": uuid.uuid4(), "t": B})
    # Same single pooled connection is reused: context must not leak.
    async with app.tenant_session(A, ACTOR) as s:
        bodies = (await s.execute(text("SELECT body FROM notes"))).scalars().all()
    assert bodies == ["a-secret"]
    async with app.tenant_session(B, ACTOR) as s:
        assert (await s.execute(text("SELECT body FROM notes"))).scalars().all() == ["b-secret"]


async def test_no_context_sees_nothing(dbs):
    _, app = dbs
    async with app.global_session() as s:
        assert (await s.execute(text("SELECT count(*) FROM notes"))).scalar_one() == 0
    async with app.engine.connect() as c:  # raw connection with no context at all
        assert (await c.execute(text("SELECT count(*) FROM notes"))).scalar_one() == 0


async def test_cross_tenant_write_rejected(dbs):
    _, app = dbs
    with pytest.raises(DBAPIError):
        async with app.tenant_session(A, ACTOR) as s:
            await s.execute(text("INSERT INTO notes VALUES (:i, :t, 'smuggled')"), {"i": uuid.uuid4(), "t": B})


async def test_cross_tenant_update_delete_affect_nothing(dbs):
    _, app = dbs
    async with app.tenant_session(A, ACTOR) as s:
        upd = await s.execute(text("UPDATE notes SET body='pwned' WHERE tenant_id=:t"), {"t": B})
        dele = await s.execute(text("DELETE FROM notes WHERE tenant_id=:t"), {"t": B})
        assert upd.rowcount == 0 and dele.rowcount == 0


async def test_tenant_id_immutable(dbs):
    _, app = dbs
    with pytest.raises(DBAPIError):
        async with app.tenant_session(A, ACTOR) as s:
            await s.execute(text("UPDATE notes SET tenant_id=:b"), {"b": B})


async def test_owner_is_also_subject_to_forced_rls(dbs):
    owner, _ = dbs
    async with owner.engine.connect() as c:
        assert (await c.execute(text("SELECT count(*) FROM notes"))).scalar_one() == 0


async def test_coverage_report(dbs):
    owner, _ = dbs
    async with owner.engine.connect() as c:
        cov = {t.table: t for t in await inspect_coverage(c)}
    assert cov["notes"].status == "ok"
