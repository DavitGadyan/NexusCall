"""Provider connection lifecycle for tenant and platform scopes (build.md §19.2–19.4).

create:  validate shape → write Vault → store metadata (no value) → server-side test → status
rotate:  validate shape → write a *staging* path → test the candidate → only then write it as the new KV
         version of the live path and switch metadata. A failed test deletes the staging secret and leaves the
         previous version untouched (rollback), so consumers never read an unvalidated credential.
revoke:  stop resolution first (status=revoked), then destroy every Vault version; emit
         `integration.connection.changed`.

Every step is audited with ids/enums only.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from integration_service.deps import audit, connection_changed, db, register_tenant, settings_of
from integration_service.providers.catalogue import mask, spec_for, validate_config, validate_credentials
from integration_service.providers.testers import TestResult
from integration_service.secrets import SecretsUnavailable
from integration_service.tables import (
    inbound_webhook_registry,
    platform_connections,
    provider_connections,
    secret_registry,
)
from nexus_core.context import Actor
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.vault import platform_secret_path, tenant_secret_path

log = logging.getLogger(__name__)
OWNER = "integration-service"


@dataclass(frozen=True, slots=True)
class Scope:
    """Where a connection lives: a tenant (RLS session) or the platform (global session)."""

    tenant_id: uuid.UUID | None
    owning_service: str = OWNER

    @property
    def platform(self) -> bool:
        return self.tenant_id is None

    @property
    def table(self) -> Table:
        return platform_connections if self.platform else provider_connections

    @property
    def kind(self) -> str:
        return "platform" if self.platform else "tenant"

    @asynccontextmanager
    async def session(self, app: FastAPI, actor: Actor) -> AsyncIterator[AsyncSession]:
        database = db(app)
        ctx = (
            database.global_session(actor) if self.tenant_id is None else database.tenant_session(self.tenant_id, actor)
        )
        async with ctx as s:
            yield s

    def path(self, env: str, provider: str, connection_id: uuid.UUID, *, staging: bool = False) -> str:
        if self.tenant_id is None:
            name = f"{self.owning_service}/{provider}" + ("-next" if staging else "")
            return platform_secret_path(env, name)
        cid = f"{connection_id}-next" if staging else str(connection_id)
        return tenant_secret_path(env, str(self.tenant_id), provider, cid)


def health_of(row: dict[str, Any]) -> str:
    if row["status"] == "revoked":
        return "revoked"
    if row["status"] == "disabled":
        return "disabled"
    if row["last_vault_status"] not in (None, "ok"):
        return "vault_error"
    if row["status"] == "error":
        return "failing"
    due = row.get("next_rotation_due")
    if due is not None and due <= datetime.now(UTC):
        return "rotation_overdue"
    return "healthy"


PUBLIC_FIELDS = (
    "id", "provider", "display_name", "status", "auth_type", "vault_ref", "vault_version", "account_masked",
    "granted_scopes", "config", "last_error", "last_validated_at", "last_rotated_at", "next_rotation_due",
    "last_vault_status", "last_resolved_at", "version", "created_at", "updated_at",
)  # fmt: skip


def public(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in PUBLIC_FIELDS}
    out["scope"] = row.get("scope", "default")
    out["owning_service"] = row.get("owning_service", OWNER)
    out["health"] = health_of(row)
    return out


async def _load(s: AsyncSession, scope: Scope, connection_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    stmt = select(scope.table).where(scope.table.c.id == connection_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await s.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Connection not found")
    return dict(row)


async def _registry(s: AsyncSession, scope: Scope, row: dict[str, Any], *, env: str, **extra: Any) -> None:
    values = {
        "scope": scope.kind,
        "tenant_id": scope.tenant_id,
        "connection_id": row["id"],
        "owning_service": row.get("owning_service", OWNER),
        "provider": row["provider"],
        "kind": "oauth_client" if row["provider"].endswith("_oauth_client") else "provider_credential",
        "environment": env,
        "status": row["status"],
        "vault_version": row.get("vault_version"),
        "last_rotated_at": row.get("last_rotated_at"),
        "next_rotation_due": row.get("next_rotation_due"),
        "last_vault_status": row.get("last_vault_status"),
        **extra,
    }
    stmt = insert(secret_registry).values(vault_ref=row["vault_ref"], **values)
    await s.execute(stmt.on_conflict_do_update(index_elements=["vault_ref"], set_={**values, "updated_at": func.now()}))


def _test_values(result: TestResult) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": "active" if result.ok else "error",
        "last_error": None if result.ok else result.error,
        "last_validated_at": func.now() if result.ok else None,
        "updated_at": func.now(),
    }
    if result.ok:
        values["account_masked"] = mask(result.account_id) if result.account_id else None
        if result.scopes:
            values["granted_scopes"] = sorted(set(result.scopes))
    return values


# ------------------------------------------------------------------------------------------- create
async def create_connection(
    app: FastAPI, scope: Scope, actor: Actor, *, provider: str, credentials: dict[str, SecretStr],
    config: dict[str, str], display_name: str | None, label: str = "default", auth_type: str = "static",
    test_override: TestResult | None = None,
) -> dict[str, Any]:  # fmt: skip
    spec = spec_for(provider)
    if spec.platform_only and not scope.platform:
        raise Unprocessable("This provider can only be configured at platform level")
    if scope.platform and scope.owning_service not in spec.platform_services:
        raise Unprocessable("This service cannot own a platform credential for this provider")
    validate_credentials(provider, credentials)
    validate_config(provider, config)
    cfg = settings_of(app)
    env = cfg.env.value
    t = scope.table
    connection_id = uuid.uuid4()

    async with scope.session(app, actor) as s:  # fail fast before touching Vault
        live = select(t.c.id).where(t.c.provider == provider, t.c.status != "revoked")
        live = (
            live.where(t.c.owning_service == scope.owning_service) if scope.platform else live.where(t.c.scope == label)
        )
        if (await s.execute(live)).first() is not None:
            raise Conflict("A connection for this provider already exists; rotate or revoke it instead")

    path = scope.path(env, provider, connection_id)
    version = await app.state.secrets.write(path, credentials)  # 1. Vault first
    values: dict[str, Any] = {
        "id": connection_id,
        "provider": provider,
        "display_name": display_name,
        "status": "error",
        "last_error": "not_tested",
        "auth_type": auth_type,
        "vault_ref": path,
        "vault_version": version,
        "config": config,
        "last_rotated_at": func.now(),
        "next_rotation_due": datetime.now(UTC) + timedelta(days=cfg.rotation_period_days),
        "last_vault_status": "ok",
        "created_by": str(actor)[:140],
    }
    if scope.platform:
        values["owning_service"] = scope.owning_service
    else:
        values |= {"tenant_id": scope.tenant_id, "scope": label}
    try:
        async with scope.session(app, actor) as s:  # 2. metadata only
            if scope.tenant_id is not None:
                await register_tenant(s, scope.tenant_id)
            row = dict((await s.execute(insert(t).values(**values).returning(t))).mappings().one())
            await _registry(s, scope, row, env=env)
            await audit(s, actor, "integration.connection.created", "provider_connection", connection_id,
                        tenant_id=scope.tenant_id, provider=provider, scope=scope.kind,
                        auth_type=auth_type)  # fmt: skip
    except IntegrityError as exc:  # a concurrent create won the unique index: undo our Vault write
        await app.state.secrets.destroy(path)
        raise Conflict("A connection for this provider already exists; rotate or revoke it instead") from exc

    result = test_override or await app.state.testers.test(provider, credentials, config)  # 3. server-side test
    return await _record_test(app, scope, actor, row, result, created=True)


async def _record_test(app: FastAPI, scope: Scope, actor: Actor, row: dict[str, Any], result: TestResult, *,
                       created: bool = False) -> dict[str, Any]:  # fmt: skip
    t = scope.table
    async with scope.session(app, actor) as s:
        new = dict(
            (await s.execute(update(t).where(t.c.id == row["id"]).values(**_test_values(result)).returning(t)))
            .mappings()
            .one()
        )
        await _registry(s, scope, new, env=settings_of(app).env.value)
        await audit(s, actor, "integration.connection.tested", "provider_connection", row["id"],
                    tenant_id=scope.tenant_id, outcome="success" if result.ok else "failure",
                    provider=row["provider"], error=result.error)  # fmt: skip
        if created or new["status"] != row["status"]:
            await connection_changed(s, tenant_id=scope.tenant_id, actor=actor, connection_id=row["id"],
                                     provider=row["provider"], status=new["status"])  # fmt: skip
        if result.ok and result.account_id and row["provider"] == "hubspot" and scope.tenant_id is not None:
            await _map_inbound(s, scope.tenant_id, row["id"], result.account_id)
    return new


async def _map_inbound(s: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID, account: str) -> None:
    """Trusted mapping portal → tenant for inbound webhooks. Never re-points an account owned by another tenant."""
    existing = (
        (
            await s.execute(
                select(inbound_webhook_registry).where(
                    inbound_webhook_registry.c.provider == "hubspot",
                    inbound_webhook_registry.c.external_account_id == account,
                )
            )
        )
        .mappings()
        .first()
    )
    if existing is not None and existing["tenant_id"] != tenant_id and existing["enabled"]:
        log.warning("inbound_account_already_mapped", extra={"provider": "hubspot"})
        return
    stmt = insert(inbound_webhook_registry).values(
        provider="hubspot", external_account_id=account, tenant_id=tenant_id, connection_id=connection_id
    )
    await s.execute(
        stmt.on_conflict_do_update(
            index_elements=["provider", "external_account_id"],
            set_={"tenant_id": tenant_id, "connection_id": connection_id, "enabled": True, "updated_at": func.now()},
        )
    )


# ------------------------------------------------------------------------------------------- test / status
async def test_connection(app: FastAPI, scope: Scope, actor: Actor, connection_id: uuid.UUID) -> dict[str, Any]:
    async with scope.session(app, actor) as s:
        row = await _load(s, scope, connection_id)
    if row["status"] == "revoked":
        raise Conflict("A revoked connection cannot be tested")
    try:
        values = await app.state.secrets.read(row["vault_ref"])
    except SecretsUnavailable:
        async with scope.session(app, actor) as s:
            await s.execute(
                update(scope.table).where(scope.table.c.id == connection_id).values(last_vault_status="read_failed")
            )
        raise
    result = await app.state.testers.test(row["provider"], values, dict(row["config"] or {}))
    del values
    return await _record_test(app, scope, actor, {**row, "last_vault_status": "ok"}, result)


async def set_enabled(
    app: FastAPI, scope: Scope, actor: Actor, connection_id: uuid.UUID, enabled: bool
) -> dict[str, Any]:
    t = scope.table
    if enabled:
        async with scope.session(app, actor) as s:
            row = await _load(s, scope, connection_id, lock=True)
            if row["status"] != "disabled":
                raise Conflict("Only a disabled connection can be enabled")
        # Re-enabling always re-validates: a credential may have expired while disabled.
        return await test_connection(app, scope, actor, connection_id)
    async with scope.session(app, actor) as s:
        row = await _load(s, scope, connection_id, lock=True)
        if row["status"] == "revoked":
            raise Conflict("A revoked connection cannot be disabled")
        if row["status"] == "disabled":
            return row
        new = dict(
            (
                await s.execute(
                    update(t)
                    .where(t.c.id == connection_id)
                    .values(status="disabled", version=t.c.version + 1, updated_at=func.now())
                    .returning(t)
                )
            )
            .mappings()
            .one()
        )
        await _registry(s, scope, new, env=settings_of(app).env.value)
        await audit(s, actor, "integration.connection.disabled", "provider_connection", connection_id,
                    tenant_id=scope.tenant_id, provider=row["provider"])  # fmt: skip
        await connection_changed(s, tenant_id=scope.tenant_id, actor=actor, connection_id=connection_id,
                                 provider=row["provider"], status="disabled")  # fmt: skip
    return new


# ------------------------------------------------------------------------------------------- rotate
async def rotate_connection(
    app: FastAPI, scope: Scope, actor: Actor, connection_id: uuid.UUID, credentials: dict[str, SecretStr], *,
    reason: str = "planned", test_override: TestResult | None = None,
) -> dict[str, Any]:  # fmt: skip
    cfg = settings_of(app)
    env = cfg.env.value
    t = scope.table
    async with scope.session(app, actor) as s:
        row = await _load(s, scope, connection_id)
    if row["status"] == "revoked":
        raise Conflict("A revoked connection cannot be rotated; create a new one")
    validate_credentials(row["provider"], credentials)
    staging = scope.path(env, row["provider"], connection_id, staging=True)
    await app.state.secrets.write(staging, credentials)  # 1. candidate, invisible to consumers
    result = test_override or await app.state.testers.test(row["provider"], credentials, dict(row["config"] or {}))
    if not result.ok:  # 2a. rollback: drop the candidate, keep the previous version live
        await app.state.secrets.destroy(staging)
        async with scope.session(app, actor) as s:
            await s.execute(
                update(t)
                .where(t.c.id == connection_id)
                .values(last_error="rotation_validation_failed", updated_at=func.now())
            )
            await audit(s, actor, "integration.connection.rotated", "provider_connection", connection_id,
                        tenant_id=scope.tenant_id, outcome="failure", provider=row["provider"],
                        error=result.error, rolled_back=True, reason=reason)  # fmt: skip
        raise Unprocessable(
            "The new credential failed validation; the previous credential is still in use",
            extra={"error": result.error, "rolled_back": True},
        )
    version = await app.state.secrets.write(row["vault_ref"], credentials)  # 2b. switch: new live version
    try:
        await app.state.secrets.destroy(staging)
    except SecretsUnavailable:
        log.warning("vault_staging_cleanup_failed", extra={"connection_id": str(connection_id)})
    async with scope.session(app, actor) as s:
        values = _test_values(result) | {
            "vault_version": version,
            "last_rotated_at": func.now(),
            "next_rotation_due": datetime.now(UTC) + timedelta(days=cfg.rotation_period_days),
            "last_vault_status": "ok",
            "version": t.c.version + 1,
        }
        new = dict(
            (await s.execute(update(t).where(t.c.id == connection_id).values(**values).returning(t))).mappings().one()
        )
        await _registry(s, scope, new, env=env, rotation_reminded_at=None)
        await audit(s, actor, "integration.connection.rotated", "provider_connection", connection_id,
                    tenant_id=scope.tenant_id, provider=row["provider"], vault_version=version,
                    reason=reason)  # fmt: skip
        await connection_changed(s, tenant_id=scope.tenant_id, actor=actor, connection_id=connection_id,
                                 provider=row["provider"], status=new["status"])  # fmt: skip
        if result.account_id and row["provider"] == "hubspot" and scope.tenant_id is not None:
            await _map_inbound(s, scope.tenant_id, connection_id, result.account_id)
    return new


# ------------------------------------------------------------------------------------------- revoke
async def revoke_connection(app: FastAPI, scope: Scope, actor: Actor, connection_id: uuid.UUID) -> dict[str, Any]:
    t = scope.table
    env = settings_of(app).env.value
    async with scope.session(app, actor) as s:  # 1. stop resolution immediately
        row = await _load(s, scope, connection_id, lock=True)
        if row["status"] == "revoked":
            return row
        new = dict(
            (
                await s.execute(
                    update(t)
                    .where(t.c.id == connection_id)
                    .values(status="revoked", version=t.c.version + 1, updated_at=func.now())
                    .returning(t)
                )
            )
            .mappings()
            .one()
        )
        await s.execute(
            update(inbound_webhook_registry)
            .where(inbound_webhook_registry.c.connection_id == connection_id)
            .values(enabled=False, updated_at=func.now())
        )
        await connection_changed(s, tenant_id=scope.tenant_id, actor=actor, connection_id=connection_id,
                                 provider=row["provider"], status="revoked")  # fmt: skip
        await audit(s, actor, "integration.connection.revoked", "provider_connection", connection_id,
                    tenant_id=scope.tenant_id, provider=row["provider"])  # fmt: skip
    vault_state = "deleted"
    try:  # 2. destroy every version of the secret
        await app.state.secrets.destroy(row["vault_ref"])
    except SecretsUnavailable:
        vault_state = "delete_failed"  # retried by the maintenance job; resolution is already blocked
    async with scope.session(app, actor) as s:
        new = dict(
            (
                await s.execute(
                    update(t).where(t.c.id == connection_id).values(last_vault_status=vault_state).returning(t)
                )
            )
            .mappings()
            .one()
        )
        await _registry(s, scope, new, env=env)
    return new


async def retry_failed_deletions(app: FastAPI) -> int:
    """Maintenance: finish Vault deletion for revoked secrets whose delete failed earlier."""
    async with db(app).global_session() as s:
        refs = list(
            (
                await s.execute(
                    select(secret_registry.c.vault_ref).where(
                        secret_registry.c.status == "revoked", secret_registry.c.last_vault_status == "delete_failed"
                    )
                )
            ).scalars()
        )
    done = 0
    for ref in refs:
        try:
            await app.state.secrets.destroy(ref)
        except SecretsUnavailable:
            continue
        done += 1
        async with db(app).global_session() as s:
            await s.execute(
                update(secret_registry)
                .where(secret_registry.c.vault_ref == ref)
                .values(last_vault_status="deleted", updated_at=func.now())
            )
    return done


# ------------------------------------------------------------------------------------------- resolve
async def resolve(app: FastAPI, tenant_id: uuid.UUID, actor: Actor, provider: str, label: str) -> dict[str, Any]:
    t = provider_connections
    async with db(app).tenant_session(tenant_id, actor) as s:
        row = (
            (
                await s.execute(
                    update(t)
                    .where(t.c.provider == provider, t.c.scope == label, t.c.status == "active")
                    .values(last_resolved_at=func.now())
                    .returning(t.c.id, t.c.provider, t.c.status, t.c.vault_ref, t.c.config)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("No active connection for this provider")
        await s.execute(
            update(secret_registry)
            .where(secret_registry.c.vault_ref == row["vault_ref"])
            .values(last_retrieval_at=func.now())
        )
    return {
        "connection_id": str(row["id"]),
        "provider": row["provider"],
        "status": row["status"],
        "vault_ref": row["vault_ref"],
        "config": dict(row["config"] or {}),
    }
