"""Internal, read-only RLS coverage endpoint exposed by every service (build.md §20.1).

Uses the service's `<db>_inspector` role (catalog read only). Callable only by
allow-listed internal clients (tenant-service aggregates for the platform UI).
There is deliberately no endpoint that changes policies.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from nexus_core.auth.jwt import Principal
from nexus_core.authz import require_service
from nexus_core.config import ServiceSettings
from nexus_core.rls import inspect_coverage, inspect_role


def rls_router(settings: ServiceSettings) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/rls", tags=["internal"], include_in_schema=False)
    guard = require_service(*settings.rls_scan_clients)

    # The guard is passed as a default, not inside `Annotated[...]`: with postponed annotations FastAPI
    # evaluates the annotation in module globals, where the local `guard` does not exist, and then treats
    # the parameter as a required query string (every scan failed with 422).
    @router.get("/coverage")
    async def coverage(request: Request, _principal: Principal = Depends(guard)) -> dict[str, Any]:
        url = settings.inspector_database_url
        if url is None:
            return {"service": settings.service_name, "available": False, "tables": [], "roles": []}
        app_url = make_url(settings.database_url.get_secret_value())
        engine = create_async_engine(url.get_secret_value(), pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as conn:
                tables = await inspect_coverage(conn)
                roles = await inspect_role(conn, app_url.username or "")
        finally:
            await engine.dispose()
        app_role = next((r for r in roles if r["rolname"] == app_url.username), None)
        role_issues = []
        if app_role is None:
            role_issues.append("app_role_not_found")
        else:
            if app_role["rolsuper"]:
                role_issues.append("app_role_is_superuser")
            if app_role["rolbypassrls"]:
                role_issues.append("app_role_bypasses_rls")
        return {
            "service": settings.service_name,
            "available": True,
            "database": app_url.database,
            "app_role": app_url.username,
            "role_issues": role_issues,
            "tables": [
                {
                    "table": t.table,
                    "schema": t.schema,
                    "status": t.status,
                    "rls_enabled": t.rls_enabled,
                    "rls_forced": t.rls_forced,
                    "has_tenant_id": t.has_tenant_id,
                    "policies": list(t.policies),
                    "commands": list(t.commands),
                }
                for t in tables
            ],
        }

    return router
