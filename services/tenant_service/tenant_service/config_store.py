"""Versioned settings storage (every change creates a new version; history is retained)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tenant_service.settings_schema import PLATFORM_SECTIONS, TENANT_SECTIONS, defaults_for_tenant
from tenant_service.tables import platform_settings, tenant_settings, tenants


async def platform_section(session: AsyncSession, section: str) -> dict[str, Any]:
    row = (
        await session.execute(
            select(platform_settings.c.data, platform_settings.c.version).where(
                platform_settings.c.section == section, platform_settings.c.is_current
            )
        )
    ).first()
    if row is None:
        return {**PLATFORM_SECTIONS[section]().model_dump(mode="json"), "_version": 0}
    return {**row.data, "_version": row.version}


async def all_platform(session: AsyncSession) -> dict[str, dict[str, Any]]:
    return {name: await platform_section(session, name) for name in PLATFORM_SECTIONS}


async def put_platform_section(
    session: AsyncSession, section: str, data: dict[str, Any], changed_by: uuid.UUID, expected_version: int
) -> int:
    current = (
        await session.execute(
            select(func.max(platform_settings.c.version))
            .where(platform_settings.c.section == section)
            .with_for_update()
        )
    ).scalar_one_or_none() or 0
    if current != expected_version:
        from nexus_core.errors import Conflict

        raise Conflict("Settings changed since you loaded them; reload and retry")
    await session.execute(
        update(platform_settings).where(platform_settings.c.section == section).values(is_current=False)
    )
    await session.execute(
        platform_settings.insert().values(
            section=section, version=current + 1, data=data, changed_by=changed_by, is_current=True
        )
    )
    return current + 1


async def tenant_section(session: AsyncSession, tenant_id: uuid.UUID, section: str) -> dict[str, Any]:
    row = (
        await session.execute(
            select(tenant_settings.c.data, tenant_settings.c.version).where(
                tenant_settings.c.section == section, tenant_settings.c.is_current
            )
        )
    ).first()
    if row is None:
        name = (await session.execute(select(tenants.c.name).where(tenants.c.id == tenant_id))).scalar_one()
        return {**defaults_for_tenant(section, name), "_version": 0}
    return {**row.data, "_version": row.version}


async def put_tenant_section(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    section: str,
    data: dict[str, Any],
    changed_by: uuid.UUID | None,
    expected_version: int,
) -> int:
    assert section in TENANT_SECTIONS
    current = (
        await session.execute(select(func.max(tenant_settings.c.version)).where(tenant_settings.c.section == section))
    ).scalar_one_or_none() or 0
    if current != expected_version:
        from nexus_core.errors import Conflict

        raise Conflict("Settings changed since you loaded them; reload and retry")
    await session.execute(update(tenant_settings).where(tenant_settings.c.section == section).values(is_current=False))
    await session.execute(
        tenant_settings.insert().values(
            tenant_id=tenant_id, section=section, version=current + 1, data=data, changed_by=changed_by, is_current=True
        )
    )
    return current + 1
