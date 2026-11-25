"""LOCAL DEVELOPMENT demo data: 40 obviously-fake contacts (+ leads) per demo tenant and one segment.

Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as the restricted `crm_app` role, so RLS applies.
Phones are in the reserved 555-01xx range, emails under `@example.test`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from crm_service.normalize import value_hash
from crm_service.tables import consent_records, contacts, leads, segments, suppressions
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, GLOBEX, demo_id

SEED_ACTOR = Actor("system", "dev-seed")
FIRST = ("Avery", "Blake", "Casey", "Devon", "Emery", "Finley", "Gray", "Harper", "Indy", "Jordan")
LAST = ("Adams", "Brooks", "Carter", "Diaz", "Evans", "Foster", "Garcia", "Hayes")
SEGMENT_RULES = {"op": "and", "rules": [{"field": "status", "operator": "eq", "value": "active"}]}
# (tenant, contact number offset, area code)
TENANTS = ((ACME, 0, "212"), (GLOBEX, 100, "312"))


async def seed(db: Database) -> None:
    captured = datetime(2026, 1, 15, tzinfo=UTC)
    for tenant, offset, area in TENANTS:
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            for i in range(1, 41):
                n = offset + i
                contact_id = demo_id("contact", n)
                email = f"lead{n:03d}@example.test"
                phone = f"+1{area}55501{i:02d}"
                await s.execute(
                    insert(contacts)
                    .values(
                        id=contact_id,
                        tenant_id=tenant.id,
                        first_name=FIRST[i % len(FIRST)],
                        last_name=LAST[i % len(LAST)],
                        company=f"Demo Customer {i:02d}",
                        email=email,
                        phone_e164=phone,
                        timezone=tenant.timezone,
                        country="US",
                        locale="en-US",
                        source="demo-seed",
                        created_by=str(SEED_ACTOR),
                    )
                    .on_conflict_do_nothing()
                )
                await s.execute(
                    insert(leads)
                    .values(
                        id=demo_id("lead", n),
                        tenant_id=tenant.id,
                        contact_id=contact_id,
                        status=("new", "contacted", "qualified")[i % 3],
                        source="demo-seed",
                        score=(i * 7) % 101,
                    )
                    .on_conflict_do_nothing()
                )
                if i == 37:  # withdrew phone consent
                    await s.execute(
                        insert(consent_records)
                        .values(
                            id=demo_id("consent", n),
                            tenant_id=tenant.id,
                            contact_id=contact_id,
                            channel="phone",
                            status="withdrawn",
                            lawful_basis="consent",
                            source="demo-seed",
                            captured_at=captured,
                            recorded_by=str(SEED_ACTOR),
                        )
                        .on_conflict_do_nothing()
                    )
                suppressed = {38: ("phone", phone), 39: ("email", email), 40: ("phone", phone)}.get(i)
                if suppressed:
                    channel, value = suppressed
                    await s.execute(
                        insert(suppressions)
                        .values(
                            id=demo_id("suppression", n),
                            tenant_id=tenant.id,
                            channel=channel,
                            value_hash=value_hash(value),
                            value=value,
                            contact_id=contact_id,
                            reason="opt_out" if i != 40 else "legal",
                            source_type="demo_seed",
                            created_by=str(SEED_ACTOR),
                        )
                        .on_conflict_do_nothing()
                    )
            await s.execute(
                insert(segments)
                .values(
                    id=demo_id("segment", offset + 1),
                    tenant_id=tenant.id,
                    name="All contactable leads",
                    description="Every active contact; contactability is applied when the segment is resolved.",
                    rules=SEGMENT_RULES,
                    created_by=str(SEED_ACTOR),
                )
                .on_conflict_do_nothing()
            )
