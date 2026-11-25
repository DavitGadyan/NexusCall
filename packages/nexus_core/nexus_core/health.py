"""Liveness (/healthz) and readiness (/readyz) endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], Awaitable[bool]]


def health_router(service: str, version: str, checks: dict[str, ReadinessCheck]) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": service, "version": version}

    @router.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        async def run(name: str, check: ReadinessCheck) -> tuple[str, bool]:
            try:
                return name, bool(await asyncio.wait_for(check(), timeout=2.0))
            except Exception:
                return name, False

        results = dict(await asyncio.gather(*(run(n, c) for n, c in checks.items())))
        ok = all(results.values())
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": results}, status_code=200 if ok else 503
        )

    return router
