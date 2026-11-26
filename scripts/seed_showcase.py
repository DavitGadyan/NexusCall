#!/usr/bin/env python3
"""Load the LOCAL DEVELOPMENT showcase data set: a busy month for the Acme demo tenant plus a fictional customer
base for the platform console (see `nexus_core.devshowcase`). Run it after `seed_dev.py`.

Every service that provides `<pkg>/seed_showcase.py` is seeded as its restricted application role (row-level
security applies). Idempotent and deterministic: the scenario is anchored on the UTC date of the first run (read
back from the data on later runs), so re-running changes nothing. Refuses to run unless ENV=development.

    SEED_DB_HOST=127.0.0.1 SEED_DB_PORT=47432 uv run python scripts/seed_showcase.py   # from the host
    uv run python scripts/seed_showcase.py crm_service call_service                   # only some services
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_all import SERVICES
from seed_dev import app_url
from sqlalchemy import select

from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devshowcase import ANCHOR_CONTACT_ID, TENANT, anchor_from_marker, build

ATTEMPTS = 6  # the dev Postgres occasionally restarts its backends; retry a service instead of failing the run


def _load_env_file() -> None:
    """Host mode: take the dev DB password from `.env` (values are never printed)."""
    if os.environ.get("NEXUS_DB_DEV_PASSWORD") or os.environ.get("MIGRATION_DB_PASSWORD"):
        return
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "NEXUS_DB_DEV_PASSWORD":
            os.environ["NEXUS_DB_DEV_PASSWORD"] = value.strip().strip("'\"")


async def find_anchor() -> date:
    """The anchor of an earlier run, recovered from the first showcase contact; today for a fresh database."""
    from crm_service.tables import contacts

    db = Database(app_url("crm"), pool_size=1, max_overflow=0)
    try:
        async with db.tenant_session(TENANT.id, Actor("system", "dev-showcase")) as s:
            created = (
                await s.execute(select(contacts.c.created_at).where(contacts.c.id == ANCHOR_CONTACT_ID))
            ).scalar_one_or_none()
    finally:
        await db.dispose()
    return anchor_from_marker(created) if created is not None else datetime.now(UTC).date()


async def main(only: set[str]) -> int:
    if os.environ.get("ENV", "development") != "development":
        print("refusing: demo data may only be loaded when ENV=development", file=sys.stderr)
        return 1
    os.environ.setdefault("ENV", "development")
    _load_env_file()
    anchor = await find_anchor()
    scenario = build(anchor)
    print(f"showcase anchored on {anchor.isoformat()}")
    for pkg, db in SERVICES:
        if only and pkg not in only:
            continue
        try:
            module = importlib.import_module(f"{pkg}.seed_showcase")
        except ModuleNotFoundError as exc:
            if exc.name == f"{pkg}.seed_showcase":
                continue
            raise
        for attempt in range(1, ATTEMPTS + 1):
            database = Database(app_url(db), pool_size=2, max_overflow=0)
            try:
                await module.seed(database, scenario)
                break
            except OSError as exc:  # connection refused / reset while Postgres recovers
                if attempt == ATTEMPTS:
                    raise
                print(f"  {pkg}: database unavailable ({type(exc).__name__}), retrying", file=sys.stderr)
                await asyncio.sleep(10 * attempt)
            except Exception as exc:
                text = str(exc)
                if attempt == ATTEMPTS or not ("recovery mode" in text or "terminating connection" in text
                                               or "connection was closed" in text):  # fmt: skip
                    raise
                print(f"  {pkg}: database restarting, retrying", file=sys.stderr)
                await asyncio.sleep(10 * attempt)
            finally:
                await database.dispose()
        print(f"seeded {pkg}")
    print("showcase data ready")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(set(sys.argv[1:]))))
