"""Periodic maintenance (ADR-0014): rotation reminders, Vault health checks, cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import func, or_, select, update

from integration_service.deps import db, emit_event, settings_of
from integration_service.tables import secret_registry
from nexus_core.context import Actor

SYSTEM = Actor("system", "integration-jobs")


async def rotation_reminders(app: FastAPI) -> int:
    """Notify the owners of static credentials that are due for rotation (build.md §19.4). At most weekly."""
    lead = timedelta(days=settings_of(app).rotation_reminder_days)
    now = datetime.now(UTC)
    sent = 0
    async with db(app).global_session(SYSTEM) as s:
        rows = (
            (
                await s.execute(
                    update(secret_registry)
                    .where(
                        secret_registry.c.status.in_(("active", "error")),
                        secret_registry.c.next_rotation_due <= now + lead,
                        or_(
                            secret_registry.c.rotation_reminded_at.is_(None),
                            secret_registry.c.rotation_reminded_at < now - timedelta(days=7),
                        ),
                    )
                    .values(rotation_reminded_at=func.now())
                    .returning(secret_registry)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            tenant = row["scope"] == "tenant"
            await emit_event(
                s,
                "notification.requested",
                tenant_id=row["tenant_id"] if tenant else None,
                actor=SYSTEM,
                payload={
                    "template": "credential_rotation_due",
                    "permission": "integrations:manage" if tenant else "platform:secrets:manage",
                    "resource_type": "provider_connection",
                    "resource_id": str(row["connection_id"]) if row["connection_id"] else None,
                    "link": "/integrations" if tenant else "/platform/security/secrets",
                    "channel": "in_app",
                    "occurred_at": row["next_rotation_due"].isoformat() if row["next_rotation_due"] else None,
                },
            )
            sent += 1
    return sent


async def check_vault_health(app: FastAPI, *, limit: int = 200) -> int:
    """Refresh `last_vault_status` from Vault metadata (no values are read)."""
    async with db(app).global_session(SYSTEM) as s:
        refs = list(
            (
                await s.execute(
                    select(secret_registry.c.vault_ref)
                    .where(secret_registry.c.status != "revoked")
                    .order_by(secret_registry.c.last_checked_at.asc().nullsfirst())
                    .limit(limit)
                )
            ).scalars()
        )
    for ref in refs:
        status, version = await app.state.secrets.health(ref)
        async with db(app).global_session(SYSTEM) as s:
            values = {"last_vault_status": status, "last_checked_at": func.now()}
            if version is not None:
                values["vault_version"] = version
            await s.execute(update(secret_registry).where(secret_registry.c.vault_ref == ref).values(**values))
    return len(refs)
