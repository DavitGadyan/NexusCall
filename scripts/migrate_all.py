#!/usr/bin/env python3
"""Apply Alembic migrations for every service, one at a time, as that service's *_owner role.

Run as a controlled one-off job (never concurrently from replicas; build.md §9.2).
Owner credentials come from the environment: MIGRATION_DB_HOST, MIGRATION_DB_PASSWORD (dev),
or per-service MIGRATION_DATABASE_URL_<DB> overrides (production: Vault-issued).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic.config import main as alembic_main

ROOT = Path(__file__).resolve().parents[1]
SERVICES = [
    ("tenant_service", "tenant"),
    ("audit_service", "audit"),
    ("crm_service", "crm"),
    ("campaign_service", "campaign"),
    ("call_service", "call"),
    ("voice_service", "voice"),
    ("ai_agent_service", "agent"),
    ("ai_gateway", "aigw"),
    ("mcp_registry", "mcpreg"),
    ("tool_execution", "toolexec"),
    ("booking_service", "booking"),
    ("email_service", "email"),
    ("email_inbox_service", "inbox"),
    ("integration_service", "integration"),
    ("notification_service", "notify"),
    ("analytics_service", "analytics"),
]


def url_for(db: str) -> str:
    override = os.environ.get(f"MIGRATION_DATABASE_URL_{db.upper()}")
    if override:
        return override
    host = os.environ.get("MIGRATION_DB_HOST", "postgres")
    port = os.environ.get("MIGRATION_DB_PORT", "5432")
    pw = os.environ["MIGRATION_DB_PASSWORD"]
    return f"postgresql+asyncpg://{db}_owner:{pw}@{host}:{port}/{db}"


def main(argv: list[str]) -> int:
    only = set(argv)
    for pkg, db in SERVICES:
        if only and pkg not in only and db not in only:
            continue
        ini = ROOT / "services" / pkg / "alembic.ini"
        os.environ["MIGRATION_DATABASE_URL"] = url_for(db)
        print(f"==> migrating {pkg} ({db})", flush=True)
        try:
            alembic_main(argv=["-c", str(ini), "upgrade", "head"])
        except SystemExit as exc:
            if exc.code:
                return int(exc.code)
    print("all migrations applied")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
