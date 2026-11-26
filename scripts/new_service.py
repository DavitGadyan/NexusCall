#!/usr/bin/env python3
"""Generate a NexusCall FastAPI service skeleton from the shared template.

Usage:
    python scripts/new_service.py <package_name> <db_name> "<title>" [--deps dep1,dep2] [--force]
    python scripts/new_service.py --all            # (re)generate the canonical 16 services (skips existing)

Generated skeletons expose only health/readiness/metrics and the infrastructure
tables (outbox, inbox, idempotency) until their phase adds real domain code
(ADR-0005: no placeholder business endpoints).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]

SERVICES: list[tuple[str, str, str, list[str]]] = [
    ("tenant_service", "tenant", "Tenant & Authorization Service", []),
    ("crm_service", "crm", "CRM Service", []),
    ("campaign_service", "campaign", "Campaign Service", []),
    ("call_service", "call", "Call Orchestration Service", []),
    ("voice_service", "voice", "Voice / Conversation Service", []),
    ("ai_agent_service", "agent", "AI Agent Service", ["pgvector>=0.3,<0.4"]),
    ("ai_gateway", "aigw", "AI Gateway", []),
    ("mcp_registry", "mcpreg", "MCP Registry / Tool Control Plane", ["jsonschema>=4.23,<5"]),
    ("tool_execution", "toolexec", "Tool Execution Service", ["jsonschema>=4.23,<5"]),
    ("booking_service", "booking", "Booking Service", []),
    ("email_service", "email", "Email Campaign Service", ["bleach>=6.2,<7"]),
    ("email_inbox_service", "inbox", "Email Inbox & AI Draft Service", ["bleach>=6.2,<7"]),
    ("integration_service", "integration", "Integration & Webhook Service", []),
    ("notification_service", "notify", "Notification Service", []),
    ("analytics_service", "analytics", "Analytics & Reporting Service", []),
    ("audit_service", "audit", "Audit Service", []),
]

FILES: dict[str, str] = {}

FILES["pyproject.toml"] = """[project]
name = "nexus-$dash"
version = "0.1.0"
description = "NexusCall $title"
requires-python = ">=3.12,<3.13"
dependencies = [
  "nexus-core",
  "nexus-event-schemas",
  "nexus-provider-contracts",
$extra_deps]

[tool.uv.sources]
nexus-core = { workspace = true }
nexus-event-schemas = { workspace = true }
nexus-provider-contracts = { workspace = true }

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["$pkg"]
"""

FILES["$pkg/__init__.py"] = '''"""NexusCall $title."""

SERVICE_NAME = "$dash"
DB_APP_ROLE = "${db}_app"
'''

FILES["$pkg/settings.py"] = """from __future__ import annotations

from functools import lru_cache

from nexus_core.config import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "$dash"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
"""

FILES[
    "$pkg/tables.py"
] = '''"""SQLAlchemy Core tables owned by $title. Only this service touches them (build.md §3.2)."""

from __future__ import annotations

from sqlalchemy import MetaData

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

metadata = MetaData()

outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
idempotency_keys = idempotency_table(metadata)
'''

FILES["$pkg/api/__init__.py"] = """from fastapi import APIRouter

routers: list[APIRouter] = []
"""

FILES["$pkg/main.py"] = """from __future__ import annotations

from fastapi import FastAPI

from nexus_core.app import create_service_app
from $pkg.api import routers
from $pkg.settings import Settings, get_settings
from $pkg.tables import inbox, outbox


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    return create_service_app(
        settings, title="NexusCall $title", routers=routers, outbox=outbox, inbox=inbox,
    )


app = create_app()
"""

FILES["alembic.ini"] = """[alembic]
script_location = %(here)s/migrations
prepend_sys_path = .
path_separator = os

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
"""

FILES[
    "migrations/env.py"
] = '''"""Alembic environment. Runs as the ${db}_owner role (MIGRATION_DATABASE_URL), never as the app role."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from $pkg.tables import metadata

target_metadata = metadata


def _url() -> str:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL must be set (owner role)")
    return url


def _run(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online())
'''

RAW_FILES: dict[str, str] = {}
RAW_FILES["migrations/script.py.mako"] = '''"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
'''

FILES[
    "migrations/versions/0001_baseline.py"
] = '''"""Baseline: shared SQL functions + outbox/inbox/idempotency infrastructure tables.

Revision ID: 0001
"""

from alembic import op

from nexus_core.rls import create_common_functions
from nexus_core.tables import create_infra_tables
from $pkg.tables import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "${db}_app"
INFRA = ("outbox", "inbox", "idempotency_keys")


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    create_common_functions(op)
    bind = op.get_bind()
    for name in INFRA:
        metadata.tables[name].create(bind)
    create_infra_tables(op, APP_ROLE)


def downgrade() -> None:
    for name in reversed(INFRA):
        op.execute(f"DROP TABLE IF EXISTS {name}")
'''

FILES["tests/test_smoke.py"] = """from fastapi.testclient import TestClient

from $pkg.main import create_app
from $pkg.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", outbox_relay_enabled=False, event_consumers_enabled=False)


def test_healthz_and_metrics():
    app = create_app(_settings())
    with TestClient(app) as client:
        assert client.get("/healthz").json()["service"] == "$dash"
        assert client.get("/metrics").status_code == 200
        ready = client.get("/readyz")
        assert ready.status_code in (200, 503)
        assert "database" in ready.json()["checks"]


def test_unauthenticated_api_rejected_with_problem_json():
    app = create_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/api/v1/__nonexistent__")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.headers.get("x-request-id")
"""

FILES["Dockerfile"] = """# syntax=docker/dockerfile:1.7
# NexusCall $title. Build context: repository root.
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM $${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_COMPILE_BYTECODE_TIMEOUT=600 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services/$pkg ./services/$pkg
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev --no-editable --package nexus-$dash

FROM $${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
ENV PATH="/src/.venv/bin:$$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --from=build --chown=root:root /src/.venv /src/.venv
COPY --chown=root:root services/$pkg/alembic.ini services/$pkg/alembic.ini
COPY --chown=root:root services/$pkg/migrations services/$pkg/migrations
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=5 \\
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
STOPSIGNAL SIGTERM
CMD ["uvicorn", "$pkg.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header"]
"""


def render(pkg: str, db: str, title: str, deps: list[str], force: bool) -> Path:
    dest = ROOT / "services" / pkg
    if dest.exists() and not force:
        print(f"skip {pkg} (exists)")
        return dest
    values = {
        "pkg": pkg,
        "db": db,
        "title": title,
        "dash": pkg.replace("_", "-"),
        "extra_deps": "".join(f'  "{d}",\n' for d in deps),
    }
    for rel, tmpl in FILES.items():
        path = dest / Template(rel).substitute(values)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not force:
            continue
        path.write_text(Template(tmpl).substitute(values))
    for rel, raw in RAW_FILES.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if force or not path.exists():
            path.write_text(raw)
    print(f"generated {pkg}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pkg", nargs="?")
    ap.add_argument("db", nargs="?")
    ap.add_argument("title", nargs="?")
    ap.add_argument("--deps", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.all:
        for pkg, db, title, deps in SERVICES:
            render(pkg, db, title, deps, a.force)
        return 0
    if not (a.pkg and a.db and a.title):
        ap.error("pkg, db and title are required unless --all")
    render(a.pkg, a.db, a.title, [d for d in a.deps.split(",") if d], a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
