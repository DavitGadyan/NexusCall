#!/usr/bin/env python3
"""Load LOCAL DEVELOPMENT demo data into every service that provides `<pkg>/seed.py`.

Each seed runs as that service's restricted application role (row-level security applies) and must be
idempotent. Refuses to run unless ENV=development.

    make seed            # inside the tools container
    SEED_DB_HOST=127.0.0.1 SEED_DB_PORT=47432 uv run python scripts/seed_dev.py   # from the host
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_all import SERVICES

from nexus_core.db import Database


def app_url(db: str) -> str:
    host = os.environ.get("SEED_DB_HOST") or os.environ.get("MIGRATION_DB_HOST", "postgres")
    port = os.environ.get("SEED_DB_PORT") or os.environ.get("MIGRATION_DB_PORT", "5432")
    password = os.environ.get("NEXUS_DB_DEV_PASSWORD") or os.environ["MIGRATION_DB_PASSWORD"]
    return f"postgresql+asyncpg://{db}_app:{password}@{host}:{port}/{db}"


async def main(only: set[str]) -> int:
    if os.environ.get("ENV", "development") != "development":
        print("refusing: demo data may only be loaded when ENV=development", file=sys.stderr)
        return 1
    # Dev identities use reserved domains (.test/.local); allow them for seed-time validation.
    os.environ.setdefault("ENV", "development")
    for pkg, db in SERVICES:
        if only and pkg not in only:
            continue
        try:
            module = importlib.import_module(f"{pkg}.seed")
        except ModuleNotFoundError as exc:
            if exc.name == f"{pkg}.seed":
                continue
            raise
        database = Database(app_url(db), pool_size=2, max_overflow=0)
        try:
            await module.seed(database)
            print(f"seeded {pkg}")
        finally:
            await database.dispose()
    print("demo data ready")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(set(sys.argv[1:]))))
