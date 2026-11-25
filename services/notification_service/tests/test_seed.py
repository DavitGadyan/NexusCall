from __future__ import annotations

import pytest
from sqlalchemy import func, select

from nexus_core.devseed import DEMO_TENANTS
from notification_service.seed import seed
from notification_service.tables import notifications

pytestmark = [pytest.mark.integration]


def test_seed_is_idempotent_and_tenant_scoped(run_db):
    async def go(db):
        await seed(db)
        await seed(db)
        counts = {}
        for t in DEMO_TENANTS:
            async with db.tenant_session(t.id) as s:
                counts[t.slug] = (await s.execute(select(func.count()).select_from(notifications))).scalar_one()
        return counts

    assert run_db(go) == {"acme": 4, "globex": 4}
