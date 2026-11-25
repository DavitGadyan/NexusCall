"""LOCAL DEVELOPMENT demo data: 14 days of deterministic dashboard metrics per demo tenant.

Idempotent: values are upserted (set, not added). Runs as `analytics_app`, so row-level security applies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert

from analytics_service.tables import daily_metrics
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import DEMO_TENANTS

SEED_ACTOR = Actor("system", "dev-seed")


def _values(tenant_index: int, day_index: int) -> dict[str, int]:
    scale = 2 if tenant_index == 0 else 1
    attempted = (40 + (day_index * 7) % 17) * scale
    answered = attempted * 55 // 100
    ended = attempted - 2
    accepted = (120 + (day_index * 11) % 23) * scale
    ai_requests = (60 + day_index % 9) * scale
    return {
        "calls.attempted": attempted,
        "calls.answered": answered,
        "calls.ended": ended,
        "calls.completed": answered,
        "calls.failed": ended - answered,
        "calls.opted_out": 1,
        "calls.qualified": answered // 4,
        "calls.duration_seconds": answered * 140,
        "calls.cost_micros": ended * 21_000,
        "bookings.created": answered // 6,
        "bookings.confirmed": answered // 8,
        "emails.accepted": accepted,
        "emails.delivered": accepted - 3,
        "emails.bounced": 3,
        "emails.unsubscribed": 1,
        "drafts.generated": 12 * scale,
        "drafts.approved": 9 * scale,
        "drafts.edited": 4 * scale,
        "drafts.rejected": 2 * scale,
        "drafts.sent": 9 * scale,
        "ai.requests": ai_requests,
        "ai.input_tokens": ai_requests * 1_800,
        "ai.output_tokens": ai_requests * 350,
        "ai.cost_micros": ai_requests * 6_500,
        "leads.created": 5 * scale,
    }


async def seed(db: Database) -> None:
    today = datetime.now(UTC).date()
    for ti, tenant in enumerate(DEMO_TENANTS):
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            for di in range(14):
                day = today - timedelta(days=13 - di)
                for metric, value in _values(ti, di).items():
                    stmt = insert(daily_metrics).values(
                        tenant_id=tenant.id, day=day, metric=metric, dimension="", value=value
                    )
                    await s.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["tenant_id", "day", "metric", "dimension"],
                            set_={"value": stmt.excluded.value},
                        )
                    )
