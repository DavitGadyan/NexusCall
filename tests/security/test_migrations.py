"""Global migration guard for EVERY service (build.md §5.3, §12, §20.1). Requires Docker.

For each service database, migrated from empty as the owner role:
  * the migrated schema matches `tables.py` exactly (no drift; comments included);
  * every table is either tenant-owned with ENABLED + FORCED RLS and complete policies, or explicitly
    declared platform-scoped (with a reason) / append-only in the model;
  * tenant-owned tables have `tenant_id NOT NULL`;
  * the runtime role is not a superuser, cannot bypass RLS and owns no tables.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nexus_core.rls import inspect_coverage
from nexus_core.testing import provision_and_migrate, start_postgres

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from migrate_all import SERVICES  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.security]
INFRA_PLATFORM_TABLES = {"outbox", "inbox"}


@pytest.fixture(scope="module")
def pg():
    try:
        container = start_postgres()
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    yield container
    container.stop()


def _drift(sync_conn, metadata):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    return compare_metadata(MigrationContext.configure(sync_conn, opts={"compare_type": True}), metadata)


async def _inspect(owner_url: str, db: str, metadata):
    engine = create_async_engine(owner_url)
    try:
        async with engine.connect() as conn:
            drift = await conn.run_sync(_drift, metadata)
            coverage = await inspect_coverage(conn)
            nullable = set(
                (
                    await conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
                            "AND column_name = 'tenant_id' AND is_nullable = 'YES'"
                        )
                    )
                ).scalars()
            )
            role = (
                await conn.execute(
                    text("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = :r"),
                    {"r": f"{db}_app"},
                )
            ).one()
            owned = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = :r"),
                    {"r": f"{db}_app"},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return drift, coverage, nullable, role, owned


@pytest.mark.parametrize(("pkg", "db"), SERVICES, ids=[s[0] for s in SERVICES])
def test_service_schema_is_migrated_isolated_and_drift_free(pg, pkg, db):
    extensions = ("pgcrypto", "btree_gist", "vector") if db == "agent" else ("pgcrypto", "btree_gist")
    sdb = provision_and_migrate(pg, db, ROOT / "services" / pkg, extensions=extensions)
    tables = importlib.import_module(f"{pkg}.tables")
    drift, coverage, nullable, role, owned = asyncio.run(_inspect(sdb.owner_url, db, tables.metadata))

    assert drift == [], f"{pkg}: migrations and tables.py differ: {drift}"
    assert not any(role), f"{pkg}: runtime role {db}_app has elevated attributes {tuple(role)}"
    assert owned == 0, f"{pkg}: runtime role must not own tables"

    declared_platform = set(getattr(tables, "PLATFORM_SCOPED", {})) | INFRA_PLATFORM_TABLES
    problems = []
    for t in coverage:
        if t.status == "platform_scoped":
            if t.table not in declared_platform:
                problems.append(f"{t.table}: platform_scoped in DB but not declared in PLATFORM_SCOPED")
        elif t.status != "ok":
            problems.append(f"{t.table}: {t.status}")
        elif t.table in nullable and not t.append_only:
            problems.append(f"{t.table}: tenant_id must be NOT NULL")
    assert not problems, f"{pkg}: RLS coverage problems: {problems}"
