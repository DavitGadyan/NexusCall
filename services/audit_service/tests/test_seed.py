from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from audit_service.chain import verify_rows
from audit_service.seed import seed
from audit_service.tables import audit_events
from nexus_core.db import Database
from nexus_core.devseed import DEMO_TENANTS

pytestmark = [pytest.mark.integration]


def test_seed_is_idempotent_and_chains_verify(sdb):
    async def go():
        app_db = Database(sdb.app_url, pool_size=1, max_overflow=0)
        platform_db = Database(sdb.url("platform"), pool_size=1, max_overflow=0)
        try:
            await seed(app_db)
            await seed(app_db)
            chains = {}
            async with platform_db.global_session() as s:
                for chain in [str(t.id) for t in DEMO_TENANTS] + ["platform"]:
                    rows = (
                        (
                            await s.execute(
                                select(audit_events)
                                .where(audit_events.c.chain == chain)
                                .order_by(audit_events.c.chain_seq)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    chains[chain] = rows
            return chains
        finally:
            await app_db.dispose()
            await platform_db.dispose()

    chains = asyncio.run(go())
    for t in DEMO_TENANTS:
        seeded = [r for r in chains[str(t.id)] if r["producer"] == "dev-seed"]
        assert len(seeded) == 4
        assert verify_rows(chains[str(t.id)]).ok
    assert verify_rows(chains["platform"]).ok
