from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from email_service.seed import seed
from nexus_core.db import Database
from nexus_core.devseed import ACME, GLOBEX

pytestmark = [pytest.mark.integration]


def test_seed_is_idempotent_and_tenant_scoped(sdb):
    async def go():
        db = Database(sdb.app_url, pool_size=1, max_overflow=0)
        try:
            await seed(db)
            await seed(db)
            out = {}
            for tenant in (ACME, GLOBEX):
                async with db.tenant_session(tenant.id) as s:
                    out[tenant.slug] = (
                        (await s.execute(text("SELECT domain, status FROM sending_domains"))).all(),
                        (await s.execute(text("SELECT count(*) FROM email_campaigns WHERE state = 'draft'"))).scalar(),
                        (await s.execute(text("SELECT count(*) FROM template_versions"))).scalar(),
                    )
            return out
        finally:
            await db.dispose()

    out = asyncio.run(go())
    assert [tuple(r) for r in out["acme"][0]] == [("acme.test", "verified")]
    assert [tuple(r) for r in out["globex"][0]] == [("globex.test", "verified")]
    assert out["acme"][1] == 1 and out["acme"][2] == 1
