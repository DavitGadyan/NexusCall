#!/usr/bin/env python3
"""Generate an explicit (frozen) Alembic migration for a service from its `tables.py` metadata.

    uv run python scripts/gen_migration.py crm_service "contacts, leads, consent, suppression"

Starts a throwaway PostgreSQL (testcontainers), applies the service's existing migrations as the
owner role, then runs `alembic revision --autogenerate`. The result contains explicit
`op.create_table(...)` / `op.add_column(...)` calls, so later model changes never alter history.

Autogenerate does NOT know about row-level security, grants, triggers or data seeding.
After generating, edit the new file and add them in `upgrade()` using `nexus_core.rls` helpers
(`apply_tenant_rls`, `apply_append_only_rls`, `mark_platform_scoped`, `grant_platform_table`).
`tests/security/test_migrations.py` fails if a table is left without RLS or drifts from `tables.py`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from migrate_all import SERVICES  # noqa: E402

RLS_HINT = """
    # ------------------------------------------------------------------ RLS, grants, seed data
    # TODO(required): for every new tenant-owned table call
    #     apply_tenant_rls(op, "<table>", APP_ROLE)            # or apply_append_only_rls(...)
    # and for every platform-global table
    #     mark_platform_scoped(op, "<table>", "<reason>"); grant_platform_table(op, "<table>", APP_ROLE, "SELECT")
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("service", help="service package, e.g. crm_service")
    ap.add_argument("message", help="short migration title")
    ap.add_argument("--rev", help="revision id (default: next 4-digit number)")
    args = ap.parse_args()

    by_pkg = dict(SERVICES)
    if args.service not in by_pkg:
        ap.error(f"unknown service {args.service}; known: {', '.join(by_pkg)}")
    db = by_pkg[args.service]
    service_dir = ROOT / "services" / args.service
    versions = service_dir / "migrations" / "versions"
    existing = sorted(p.name for p in versions.glob("[0-9]*.py"))
    rev = args.rev or f"{len(existing) + 1:04d}"

    from alembic.config import main as alembic_main

    from nexus_core.testing import provision_and_migrate, start_postgres

    container = start_postgres()
    try:
        extensions = ("pgcrypto", "btree_gist", "vector") if db == "agent" else ("pgcrypto", "btree_gist")
        sdb = provision_and_migrate(container, db, service_dir, extensions=extensions)
        os.environ["MIGRATION_DATABASE_URL"] = sdb.owner_url
        slug = re.sub(r"[^a-z0-9]+", "_", args.message.lower()).strip("_")[:40]
        before = set(versions.glob("*.py"))
        alembic_main(
            argv=[
                "-c",
                str(service_dir / "alembic.ini"),
                "revision",
                "--autogenerate",
                "-m",
                args.message,
                "--rev-id",
                rev,
            ]
        )
        created = list(set(versions.glob("*.py")) - before)
        if not created:
            print("no changes detected")
            return 0
        target = versions / f"{rev}_{slug}.py"
        created[0].rename(target)
        text = target.read_text()
        header = (
            "from nexus_core.rls import (  # noqa: F401\n"
            "    apply_append_only_rls, apply_tenant_rls, grant_platform_table, mark_platform_scoped,\n)\n"
        )
        if "pgvector." in text:
            header = "import pgvector.sqlalchemy  # noqa: F401\n" + header
        text = text.replace("\nrevision = ", f'\n{header}\nAPP_ROLE = "{db}_app"\n\nrevision = ', 1)
        text = re.sub(
            r"(\n    # ### end Alembic commands ###\n)(\n\ndef downgrade)", r"\1" + RLS_HINT + r"\2", text, count=1
        )
        target.write_text(text)
        print(f"wrote {target.relative_to(ROOT)}  (now add RLS/grants where marked TODO)")
    finally:
        container.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
