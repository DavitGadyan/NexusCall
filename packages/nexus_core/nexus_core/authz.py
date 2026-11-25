"""Authorization dependencies: tenant membership + RBAC permission checks (build.md §5.2).

`X-Tenant-ID` only *selects* a tenant. Membership and permissions are always
resolved server-side by tenant-service (or a local resolver inside tenant-service)
and cached briefly in Redis, keyed by a per-tenant epoch that tenant-service bumps
on every membership/role change so revocations take effect immediately.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Protocol

import httpx
from fastapi import Depends, Header, Request
from redis.asyncio import Redis

from nexus_core.auth.jwt import Principal, TokenVerifier
from nexus_core.context import Actor, actor_var, tenant_id_var
from nexus_core.errors import Forbidden, StepUpRequired, Unauthorized

PLATFORM_ROLES = frozenset({"platform_owner", "platform_operator"})


@dataclass(frozen=True, slots=True)
class ResolvedAccess:
    is_member: bool
    permissions: frozenset[str]
    roles: frozenset[str] = field(default_factory=frozenset)
    tenant_status: str = "active"


class AuthzResolver(Protocol):
    async def resolve_tenant(self, principal: Principal, tenant_id: uuid.UUID) -> ResolvedAccess: ...

    async def resolve_platform(self, principal: Principal) -> frozenset[str]: ...


def has_permission(granted: frozenset[str], required: str) -> bool:
    """Supports exact matches and trailing wildcards, e.g. `platform:*` or `emails:*`."""
    if required in granted:
        return True
    parts = required.split(":")
    return any(":".join([*parts[: i - 1], "*"]) in granted for i in range(len(parts), 0, -1))


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal: Principal
    tenant_id: uuid.UUID | None
    permissions: frozenset[str]
    platform_permissions: frozenset[str]
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def actor(self) -> Actor:
        return Actor(type="service" if self.principal.is_service else "user", id=self.principal.sub)

    def can(self, permission: str) -> bool:
        pool = self.platform_permissions if permission.startswith("platform:") else self.permissions
        return has_permission(pool, permission)

    def require_tenant(self) -> uuid.UUID:
        if self.tenant_id is None:
            raise Forbidden("A tenant context (X-Tenant-ID) is required")
        return self.tenant_id


class HttpAuthzResolver:
    """Calls tenant-service `/internal/v1/authz/*`; caches results in Redis."""

    def __init__(
        self,
        base_url: str,
        token_source: Callable[[], Awaitable[str]],
        redis: Redis | None,
        ttl_seconds: int = 30,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token_source
        self._redis = redis
        self._ttl = ttl_seconds

    async def _epoch(self, tenant_id: uuid.UUID) -> str:
        if self._redis is None:
            return "0"
        value = await self._redis.get(f"authz_epoch:{tenant_id}")
        return value.decode() if isinstance(value, bytes) else str(value or "0")

    async def resolve_tenant(self, principal: Principal, tenant_id: uuid.UUID) -> ResolvedAccess:
        cache_key = None
        if self._redis is not None:
            cache_key = f"authz:{tenant_id}:{principal.sub}:{await self._epoch(tenant_id)}"
            cached = await self._redis.get(cache_key)
            if cached:
                data = json.loads(cached)
                return ResolvedAccess(
                    data["is_member"], frozenset(data["permissions"]), frozenset(data["roles"]), data["tenant_status"]
                )
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{self._base}/internal/v1/authz/resolve",
                json={"sub": principal.sub, "tenant_id": str(tenant_id)},
                headers={"Authorization": f"Bearer {await self._token()}"},
            )
        resp.raise_for_status()
        data = resp.json()
        if cache_key is not None and self._redis is not None:
            await self._redis.set(cache_key, json.dumps(data), ex=self._ttl)
        return ResolvedAccess(
            data["is_member"], frozenset(data["permissions"]), frozenset(data["roles"]), data["tenant_status"]
        )

    async def resolve_platform(self, principal: Principal) -> frozenset[str]:
        if not (principal.realm_roles & PLATFORM_ROLES):
            return frozenset()
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{self._base}/internal/v1/authz/platform",
                json={"sub": principal.sub, "realm_roles": sorted(principal.realm_roles)},
                headers={"Authorization": f"Bearer {await self._token()}"},
            )
        resp.raise_for_status()
        return frozenset(resp.json()["permissions"])


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def get_principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
    verifier: TokenVerifier = request.app.state.token_verifier
    principal = await verifier.verify(_bearer(authorization))
    actor_var.set(f"{'service' if principal.is_service else 'user'}:{principal.sub}")
    return principal


async def get_auth_context(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    x_tenant_id: Annotated[str | None, Header()] = None,
) -> AuthContext:
    resolver: AuthzResolver = request.app.state.authz_resolver
    platform_perms = await resolver.resolve_platform(principal)
    if x_tenant_id is None:
        return AuthContext(principal, None, frozenset(), platform_perms)
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as exc:
        raise Forbidden("Invalid tenant identifier") from exc
    access = await resolver.resolve_tenant(principal, tenant_id)
    if not access.is_member:
        # Same response whether the tenant exists or not: no tenant enumeration.
        raise Forbidden("No access to this tenant")
    if access.tenant_status != "active":
        raise Forbidden("Tenant is not active")
    tenant_id_var.set(str(tenant_id))
    return AuthContext(principal, tenant_id, access.permissions, platform_perms, access.roles)


AuthDep = Annotated[AuthContext, Depends(get_auth_context)]


def require_permission(permission: str) -> Callable[..., Awaitable[AuthContext]]:
    async def _dep(ctx: AuthDep) -> AuthContext:
        if not permission.startswith("platform:"):
            ctx.require_tenant()
        if not ctx.can(permission):
            raise Forbidden(f"Missing permission: {permission}")
        return ctx

    _dep.__name__ = f"require_{permission.replace(':', '_').replace('*', 'any')}"
    return _dep


def require_step_up(max_age_seconds: int = 300) -> Callable[..., Awaitable[AuthContext]]:
    """High-risk actions require a recent MFA-backed authentication (build.md §20.2)."""

    async def _dep(ctx: AuthDep) -> AuthContext:
        p = ctx.principal
        if p.is_service:
            raise Forbidden("Step-up actions require a human user")
        if p.acr not in {"aal2", "aal3", "2"} or not p.authenticated_within(max_age_seconds):
            raise StepUpRequired(
                "Re-authenticate with MFA to perform this action",
                extra={"required_acr": "aal2", "max_age": max_age_seconds},
            )
        return ctx

    return _dep


def require_service(*allowed_clients: str) -> Callable[..., Awaitable[Principal]]:
    """Guard for /internal endpoints: caller must be an allow-listed service client."""

    async def _dep(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.is_service or principal.azp not in allowed_clients:
            raise Forbidden("Caller is not an authorized internal service")
        return principal

    return _dep


@dataclass(frozen=True, slots=True)
class InternalContext:
    """Verified caller + asserted tenant/actor for /internal endpoints.

    The calling service is authenticated (service JWT + `azp` allowlist). The tenant and actor it asserts
    are used to open an RLS-scoped session; any referenced resource must still be loaded inside that
    session, so a wrong tenant simply finds nothing (404)."""

    principal: Principal
    tenant_id: uuid.UUID | None
    actor: Actor

    def require_tenant(self) -> uuid.UUID:
        if self.tenant_id is None:
            raise Forbidden("X-Tenant-ID is required for this internal operation")
        return self.tenant_id


def _parse_actor(raw: str | None, fallback: Actor) -> Actor:
    if not raw:
        return fallback
    kind, _, ident = raw.partition(":")
    if kind not in ("user", "service", "system") or not ident or len(ident) > 128:
        raise Forbidden("Invalid X-Actor header")
    return Actor(type=kind, id=ident)


def require_internal(*allowed_clients: str) -> Callable[..., Awaitable[InternalContext]]:
    """Guard for /internal endpoints that act within a tenant on behalf of an actor."""
    service_guard = require_service(*allowed_clients)

    # NOTE: `service_guard` is a closure local. With postponed annotations FastAPI cannot resolve
    # `Annotated[..., Depends(<local>)]`, so the dependency is passed as a default value instead.
    async def _dep(
        principal: Principal = Depends(service_guard),
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_actor: Annotated[str | None, Header()] = None,
    ) -> InternalContext:
        tenant_id: uuid.UUID | None = None
        if x_tenant_id is not None:
            try:
                tenant_id = uuid.UUID(x_tenant_id)
            except ValueError as exc:
                raise Forbidden("Invalid tenant identifier") from exc
            tenant_id_var.set(str(tenant_id))
        actor = _parse_actor(x_actor, Actor(type="service", id=principal.azp or principal.sub))
        actor_var.set(str(actor))
        return InternalContext(principal, tenant_id, actor)

    return _dep
