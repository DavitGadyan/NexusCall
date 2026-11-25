"""CRM domain operations shared by the public API, the internal API, event consumers and import jobs.

All functions run inside a caller-provided tenant session (RLS applies). They never log addresses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from crm_service.deps import audit, emit
from crm_service.normalize import (
    InvalidAddress,
    contact_marker_hash,
    normalize_email,
    normalize_phone,
    phone_country,
    value_hash,
)
from crm_service.tables import consent_records, contact_tags, contacts, leads, suppressions, tags
from nexus_core.context import Actor
from nexus_core.errors import NotFound, Unprocessable

CONTACT_FIELDS = (
    "first_name",
    "last_name",
    "company",
    "organization_id",
    "email",
    "phone_e164",
    "timezone",
    "country",
    "locale",
    "status",
    "owner_id",
    "source",
    "custom_fields",
)
OPEN_LEAD_STATUSES = ("new", "contacted", "qualified", "unqualified")


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def tag_names(session: AsyncSession, contact_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    ids = list(contact_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(contact_tags.c.contact_id, tags.c.name)
            .select_from(contact_tags.join(tags, tags.c.id == contact_tags.c.tag_id))
            .where(contact_tags.c.contact_id.in_(ids))
            .order_by(tags.c.name)
        )
    ).all()
    out: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    for r in rows:
        out[r.contact_id].append(r.name)
    return out


async def load_contact(session: AsyncSession, contact_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    stmt = select(contacts).where(contacts.c.id == contact_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Contact not found")
    return dict(row)


def normalize_addresses(email: str | None, phone: str | None, region: str | None) -> tuple[str | None, str | None]:
    try:
        return (
            normalize_email(email) if email else None,
            normalize_phone(phone, region) if phone else None,
        )
    except InvalidAddress as exc:
        raise Unprocessable(f"Invalid address: {exc}") from exc


async def find_by_addresses(
    session: AsyncSession, email: str | None, phone: str | None, *, lock: bool = False
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    async def one(col: Any, value: str | None) -> dict[str, Any] | None:
        if value is None:
            return None
        stmt = select(contacts).where(col == value)
        if lock:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    return await one(contacts.c.email, email), await one(contacts.c.phone_e164, phone)


async def insert_contact(
    session: AsyncSession, *, tenant_id: uuid.UUID, values: dict[str, Any], actor: Actor
) -> dict[str, Any]:
    data = {k: v for k, v in values.items() if k in CONTACT_FIELDS}
    if data.get("phone_e164") and not data.get("country"):
        data["country"] = phone_country(data["phone_e164"])
    row = (
        (
            await session.execute(
                insert(contacts).values(tenant_id=tenant_id, created_by=str(actor), **data).returning(contacts)
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def update_contact(
    session: AsyncSession, contact_id: uuid.UUID, changes: dict[str, Any], *, expected_version: int | None = None
) -> dict[str, Any] | None:
    """Optimistic update; returns None when the version did not match."""
    stmt = update(contacts).where(contacts.c.id == contact_id)
    if expected_version is not None:
        stmt = stmt.where(contacts.c.version == expected_version)
    row = (
        (
            await session.execute(
                stmt.values(**changes, version=contacts.c.version + 1, updated_at=func.now()).returning(contacts)
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def contact_updated(session: AsyncSession, *, tenant_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID) -> None:
    await emit(session, "contact.updated", tenant_id=tenant_id, actor=actor, payload={"contact_id": str(contact_id)})


async def create_lead(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    actor: Actor,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                insert(leads)
                .values(tenant_id=tenant_id, contact_id=contact_id, created_by=str(actor), **(values or {}))
                .returning(leads)
            )
        )
        .mappings()
        .one()
    )
    await emit(
        session,
        "lead.created",
        tenant_id=tenant_id,
        actor=actor,
        payload={"contact_id": str(contact_id), "lead_id": str(row["id"])},
    )
    return dict(row)


async def latest_lead(session: AsyncSession, contact_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                select(leads)
                .where(leads.c.contact_id == contact_id)
                .order_by(leads.c.created_at.desc(), leads.c.id.desc())
                .limit(1)
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def add_consent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: str,
    status: str,
    lawful_basis: str,
    source: str,
    actor: Actor,
    captured_at: datetime | None = None,
    source_ref: str | None = None,
    policy_version: str | None = None,
    evidence_ref: str | None = None,
) -> uuid.UUID:
    return (  # type: ignore[no-any-return]
        await session.execute(
            insert(consent_records)
            .values(
                tenant_id=tenant_id,
                contact_id=contact_id,
                channel=channel,
                status=status,
                lawful_basis=lawful_basis,
                source=source,
                source_ref=source_ref,
                captured_at=captured_at or datetime.now(UTC),
                policy_version=policy_version,
                evidence_ref=evidence_ref,
                recorded_by=str(actor),
            )
            .returning(consent_records.c.id)
        )
    ).scalar_one()


def _normalize_suppression_value(channel: str, raw: str, region: str | None) -> tuple[str, str]:
    """Returns (normalized value, address kind)."""
    try:
        if channel == "email" or (channel == "all" and "@" in raw):
            return normalize_email(raw), "email"
        return normalize_phone(raw, region), "phone"
    except InvalidAddress as exc:
        raise Unprocessable(f"Invalid suppression value: {exc}") from exc


async def add_suppression(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    reason: str,
    source_type: str,
    source_id: str | None,
    actor: Actor,
    contact_id: uuid.UUID | None = None,
    value: str | None = None,
    value_hash_override: str | None = None,
    region: str | None = "US",
) -> tuple[uuid.UUID, bool]:
    """Idempotent on (tenant, channel, value hash). Returns (suppression id, created)."""
    contact = await load_contact(session, contact_id) if contact_id is not None else None
    normalized: str | None = None
    if value:
        normalized, _ = _normalize_suppression_value(channel, value, region)
    elif contact is not None and channel in ("phone", "email"):
        normalized = contact["phone_e164"] if channel == "phone" else contact["email"]
    if value_hash_override is not None:
        digest = value_hash_override
    elif normalized is not None:
        digest = value_hash(normalized)
    elif contact is not None:
        digest = contact_marker_hash(contact["id"])
    else:
        raise Unprocessable("A suppression needs a value or a contact")
    inserted = (
        await session.execute(
            insert(suppressions)
            .values(
                tenant_id=tenant_id,
                channel=channel,
                value_hash=digest,
                value=normalized,
                contact_id=contact["id"] if contact else None,
                reason=reason,
                source_type=source_type,
                source_id=source_id,
                created_by=str(actor),
            )
            .on_conflict_do_nothing(constraint="uq_suppressions_value")
            .returning(suppressions.c.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        existing = (
            await session.execute(
                select(suppressions.c.id).where(suppressions.c.channel == channel, suppressions.c.value_hash == digest)
            )
        ).scalar_one()
        return existing, False
    payload: dict[str, Any] = {"suppression_id": str(inserted), "channel": channel, "reason": reason}
    if contact is not None:
        payload["contact_id"] = str(contact["id"])
    await emit(session, "suppression.added", tenant_id=tenant_id, actor=actor, payload=payload)
    await audit(
        session,
        actor,
        "suppression.added",
        "suppression",
        inserted,
        tenant_id=tenant_id,
        channel=channel,
        reason=reason,
        source_type=source_type,
    )
    return inserted, True
