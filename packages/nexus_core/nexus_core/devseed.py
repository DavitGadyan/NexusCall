"""Fixed identifiers for LOCAL DEVELOPMENT demo data (shared by every service's `seed.py`).

User ids match the Keycloak dev realm (`scripts/gen_keycloak_realm.py`). Demo data must be obviously
fake: emails under `@example.test`, phone numbers in the reserved +1 555 01xx range.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SeedTenant:
    id: uuid.UUID
    slug: str
    name: str
    timezone: str


@dataclass(frozen=True, slots=True)
class SeedUser:
    id: uuid.UUID
    email: str
    name: str
    platform_role: str | None = None
    tenant_roles: tuple[tuple[str, str], ...] = ()  # (tenant slug, role key)


ACME = SeedTenant(uuid.UUID("0a000000-0000-4000-8000-000000000001"), "acme", "Acme Home Services", "America/New_York")
GLOBEX = SeedTenant(uuid.UUID("0a000000-0000-4000-8000-000000000002"), "globex", "Globex Dental", "America/Chicago")
DEMO_TENANTS: tuple[SeedTenant, ...] = (ACME, GLOBEX)


def _u(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-4000-8000-{n:012d}")


DEMO_USERS: tuple[SeedUser, ...] = (
    SeedUser(_u(1), "platform.owner@nexus.local", "Paula Owner", platform_role="platform_owner"),
    SeedUser(_u(2), "platform.operator@nexus.local", "Oscar Operator", platform_role="platform_operator"),
    SeedUser(_u(101), "owner@acme.test", "Ada Acme-Owner", tenant_roles=(("acme", "tenant_owner"),)),
    SeedUser(_u(102), "admin@acme.test", "Alan Acme-Admin", tenant_roles=(("acme", "tenant_admin"),)),
    SeedUser(_u(103), "campaigns@acme.test", "Cara Campaigns", tenant_roles=(("acme", "campaign_manager"),)),
    SeedUser(_u(104), "agent@acme.test", "Ari Agent", tenant_roles=(("acme", "call_agent"),)),
    SeedUser(_u(105), "email@acme.test", "Emma Email", tenant_roles=(("acme", "email_manager"),)),
    SeedUser(_u(106), "analyst@acme.test", "Ana Analyst", tenant_roles=(("acme", "analyst"),)),
    SeedUser(_u(201), "owner@globex.test", "Gus Globex-Owner", tenant_roles=(("globex", "tenant_owner"),)),
    SeedUser(_u(301), "multi@both.test", "Max Multi", tenant_roles=(("acme", "analyst"), ("globex", "tenant_admin"))),
)


def demo_id(namespace: str, n: int) -> uuid.UUID:
    """Deterministic ids for demo rows so seeds are idempotent and services can cross-reference them.

    Shared namespaces: `contact` 1..40 per tenant index (acme: 1..40, globex: 101..140), `agent` 1 (voice)
    and 2 (email) per tenant index offset (acme: 1,2; globex: 101,102), `segment` 1, `mailbox` 1."""
    return uuid.uuid5(uuid.UUID("6f1d7b5e-3c1a-4f0e-9c56-000000000000"), f"{namespace}:{n}")
