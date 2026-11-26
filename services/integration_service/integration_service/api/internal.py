"""`GET /internal/v1/connections/resolve?provider=` (BUILD_CONVENTIONS §5): the Vault *reference* of the
tenant's active connection. Callers read the secret from Vault under their own identity and policy; this
endpoint never returns a value. Disabled, failing and revoked connections are not resolvable (404)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from integration_service import connections as lifecycle
from integration_service.api.models import ResolveOut
from integration_service.settings import get_settings
from nexus_core.authz import InternalContext, require_internal

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)
guard = require_internal(*get_settings().internal_client_allowlist)


@router.get("/connections/resolve", response_model=ResolveOut)
async def resolve(
    request: Request,
    provider: str = Query(min_length=2, max_length=32, pattern=r"^[a-z_]+$"),
    scope: str = Query("default", min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$"),
    ctx: InternalContext = Depends(guard),
) -> dict[str, Any]:
    return await lifecycle.resolve(request.app, ctx.require_tenant(), ctx.actor, provider, scope)
