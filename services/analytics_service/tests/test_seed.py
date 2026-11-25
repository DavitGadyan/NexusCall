from __future__ import annotations

import pytest
from sqlalchemy import func, select

from analytics_service.seed import seed
from analytics_service.tables import daily_metrics
from nexus_core.devseed import DEMO_TENANTS

pytestmark = [pytest.mark.integration]


def test_seed_is_idempotent_and_tenant_scoped(run_db):
    async def go(db):
        await seed(db)
        await seed(db)
        out = {}
        for t in DEMO_TENANTS:
            async with db.tenant_session(t.id) as s:
                out[t.slug] = (
                    await s.execute(
                        select(func.sum(daily_metrics.c.value)).where(daily_metrics.c.metric == "calls.attempted")
                    )
                ).scalar_one()
        return out

    totals = run_db(go)
    assert totals["acme"] == 2 * totals["globex"] > 0  # acme is seeded at twice the volume; no doubling on re-run
