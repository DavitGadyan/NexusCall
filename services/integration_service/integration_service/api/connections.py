"""Tenant provider connections (`integrations:read` / `integrations:manage`). Create, rotate and revoke need
step-up MFA and are audited. No response ever contains a credential."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from integration_service import connections as lifecycle
from integration_service import oauth
from integration_service.api.models import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionPage,
    OAuthStartOut,
    ProviderList,
    RotateIn,
)
from integration_service.connections import Scope, public
from integration_service.deps import db, settings_of
from integration_service.providers.catalogue import PROVIDERS
from integration_service.secrets import fingerprint
from integration_service.tables import idempotency_keys, provider_connections
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import Unprocessable
from nexus_core.idempotency import IdempotencyStore
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.ratelimit import enforce_fixed_window

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])
callback_router = APIRouter(tags=["oauth"])
Read = Annotated[AuthContext, Depends(require_permission("integrations:read"))]
Manage = Annotated[AuthContext, Depends(require_permission("integrations:manage"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
store = IdempotencyStore(idempotency_keys)


@router.get("/providers", response_model=ProviderList)
async def list_providers(_: Read) -> dict[str, Any]:
    return {
        "items": [
            {
                "provider": p.key,
                "credential_fields": sorted(p.fields),
                "optional_fields": sorted(p.optional_fields),
                "config_keys": sorted(p.config_keys),
                "oauth": p.oauth,
                "platform_only": p.platform_only,
                "platform_services": sorted(p.platform_services),
            }
            for p in PROVIDERS.values()
        ]
    }


@router.get("/connections", response_model=ConnectionPage)
async def list_connections(
    request: Request, ctx: Read, limit: Limit = 50, cursor: Cursor = None, provider: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    t = provider_connections
    stmt = select(t)
    if provider:
        stmt = stmt.where(t.c.provider == provider)
    if status:
        stmt = stmt.where(t.c.status == status)
    stmt = apply_keyset(stmt, t.c.created_at, t.c.id, cursor, limit)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    return {"items": [public(r) for r in page], "next_cursor": next_cursor}


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: uuid.UUID, request: Request, ctx: Read) -> dict[str, Any]:
    scope = Scope(ctx.require_tenant())
    async with scope.session(request.app, ctx.actor) as s:
        return public(await lifecycle._load(s, scope, connection_id))


@router.post("/connections", response_model=ConnectionOut, status_code=201)
async def create_connection(
    body: ConnectionCreate, request: Request, response: Response, ctx: Manage, _: StepUp,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:  # fmt: skip
    if not idempotency_key:
        raise Unprocessable("Idempotency-Key header is required")
    tenant_id = ctx.require_tenant()
    # The request hash covers a keyed HMAC of the credentials, never the values themselves.
    payload = {
        **body.model_dump(exclude={"credentials"}),
        "credentials": fingerprint(request.app.state.hmac_key, body.credentials),
    }
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        outcome = await store.begin(s, tenant_id=tenant_id, operation="connection.create", key=idempotency_key,
                                    payload=payload)  # fmt: skip
    if not outcome.is_new:
        response.status_code = int(outcome.response_status or 201)
        return dict(outcome.response_body or {})
    try:
        row = await lifecycle.create_connection(
            request.app, Scope(tenant_id), ctx.actor, provider=body.provider, credentials=body.credentials,
            config=body.config, display_name=body.display_name, label=body.scope,
        )  # fmt: skip
    except Exception:
        async with db(request).tenant_session(tenant_id, ctx.actor) as s:
            await store.fail_retryable(s, outcome.record_id)
        raise
    out = ConnectionOut.model_validate(public(row)).model_dump(mode="json")
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await store.succeed(s, outcome.record_id, status=201, body=out, resource_id=str(row["id"]))
    return out


@router.post("/connections/{connection_id}/test", response_model=ConnectionOut)
async def test_connection(connection_id: uuid.UUID, request: Request, ctx: Manage) -> dict[str, Any]:
    await enforce_fixed_window(getattr(request.app.state, "redis", None),
                               f"conn-test:{ctx.require_tenant()}:{connection_id}", 10, 60)  # fmt: skip
    return public(await lifecycle.test_connection(request.app, Scope(ctx.require_tenant()), ctx.actor, connection_id))


@router.post("/connections/{connection_id}/rotate", response_model=ConnectionOut)
async def rotate_connection(
    connection_id: uuid.UUID, body: RotateIn, request: Request, ctx: Manage, _: StepUp
) -> dict[str, Any]:
    return public(
        await lifecycle.rotate_connection(
            request.app, Scope(ctx.require_tenant()), ctx.actor, connection_id, body.credentials, reason=body.reason
        )
    )


@router.post("/connections/{connection_id}/disable", response_model=ConnectionOut)
async def disable_connection(connection_id: uuid.UUID, request: Request, ctx: Manage) -> dict[str, Any]:
    return public(
        await lifecycle.set_enabled(request.app, Scope(ctx.require_tenant()), ctx.actor, connection_id, False)
    )


@router.post("/connections/{connection_id}/enable", response_model=ConnectionOut)
async def enable_connection(connection_id: uuid.UUID, request: Request, ctx: Manage) -> dict[str, Any]:
    return public(await lifecycle.set_enabled(request.app, Scope(ctx.require_tenant()), ctx.actor, connection_id, True))


@router.post("/connections/{connection_id}/revoke", response_model=ConnectionOut)
async def revoke_connection(connection_id: uuid.UUID, request: Request, ctx: Manage, _: StepUp) -> dict[str, Any]:
    return public(await lifecycle.revoke_connection(request.app, Scope(ctx.require_tenant()), ctx.actor, connection_id))


# ------------------------------------------------------------------------------------------- OAuth
@router.get("/oauth/{provider}/start", response_model=OAuthStartOut)
async def oauth_start(provider: str, request: Request, response: Response, ctx: Manage, _: StepUp) -> dict[str, Any]:
    url, binding, expires = await oauth.start(request.app, ctx.require_tenant(), ctx.actor, provider)
    s = settings_of(request)
    response.set_cookie(
        oauth.BIND_COOKIE,
        binding,
        max_age=s.oauth_state_ttl_seconds,
        path=s.oauth_redirect_path,
        httponly=True,
        secure=s.is_production_like or s.public_base_url.startswith("https://"),
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"authorization_url": url, "expires_at": expires}


@callback_router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(
    request: Request, state: str | None = None, code: str | None = None, error: str | None = None
) -> Response:
    """Browser redirect target (no bearer token: the server-side state is the authority)."""
    s = settings_of(request)
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or getattr(request.client, "host", "?")
    await enforce_fixed_window(getattr(request.app.state, "redis", None), f"oauth-cb:{ip}", 30, 60)
    result = await oauth.callback(
        request.app, state=state, code=code, error=error, binding=request.cookies.get(oauth.BIND_COOKIE)
    )
    query = f"oauth={result['status']}&provider={result['provider']}"
    if result["connection_id"]:
        query += f"&connection={result['connection_id']}"
    redirect = RedirectResponse(f"{s.public_base_url.rstrip('/')}{s.oauth_success_path}?{query}", status_code=303)
    redirect.delete_cookie(oauth.BIND_COOKIE, path=s.oauth_redirect_path)
    redirect.headers["Cache-Control"] = "no-store"
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect
