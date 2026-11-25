"""Contactability (build.md §3.1E, §11). A contact is NOT contactable on a channel when it is:

* suppressed for the channel or for `all` (by address hash or by contact id),
* its latest consent record for the channel is `withdrawn`,
* it has no address for the channel,
* it is not active.

`evaluate` computes per-contact reasons in Python (single lookups and batches); `contactable_clause` is the
same rule as a SQL expression (segment resolution). Tests check that both agree.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, String, and_, cast, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from crm_service.normalize import contact_marker_hash, value_hash
from crm_service.tables import consent_records, contacts, suppressions

CHANNELS = ("phone", "email")


def address_of(contact: Mapping[str, Any], channel: str) -> str | None:
    return contact["email"] if channel == "email" else contact["phone_e164"]


async def evaluate(
    session: AsyncSession, rows: Sequence[Mapping[str, Any]], channel: str
) -> dict[uuid.UUID, list[str]]:
    """Reasons per contact id (empty list = contactable). Two queries regardless of batch size."""
    if not rows:
        return {}
    ids = [r["id"] for r in rows]
    hashes: set[str] = {contact_marker_hash(i) for i in ids}
    for r in rows:
        if (addr := address_of(r, channel)) is not None:
            hashes.add(value_hash(addr))
    supp = (
        await session.execute(
            select(suppressions.c.contact_id, suppressions.c.value_hash).where(
                suppressions.c.channel.in_((channel, "all")),
                or_(suppressions.c.value_hash.in_(hashes), suppressions.c.contact_id.in_(ids)),
            )
        )
    ).all()
    by_contact = {s.contact_id for s in supp if s.contact_id is not None}
    by_hash = {s.value_hash for s in supp}
    latest = (
        await session.execute(
            select(consent_records.c.contact_id, consent_records.c.status)
            .where(consent_records.c.contact_id.in_(ids), consent_records.c.channel == channel)
            .distinct(consent_records.c.contact_id)
            .order_by(
                consent_records.c.contact_id,
                consent_records.c.captured_at.desc(),
                consent_records.c.created_at.desc(),
            )
        )
    ).all()
    withdrawn = {c.contact_id for c in latest if c.status == "withdrawn"}
    out: dict[uuid.UUID, list[str]] = {}
    for r in rows:
        reasons: list[str] = []
        addr = address_of(r, channel)
        if (
            r["id"] in by_contact
            or contact_marker_hash(r["id"]) in by_hash
            or (addr is not None and value_hash(addr) in by_hash)
        ):
            reasons.append("suppressed")
        if r["id"] in withdrawn:
            reasons.append("no_consent")
        if addr is None:
            reasons.append("no_address")
        if r["status"] != "active":
            reasons.append("contact_inactive")
        out[r["id"]] = reasons
    return out


def _sha256_hex(expr: ColumnElement[Any]) -> ColumnElement[Any]:
    return func.encode(func.digest(expr, "sha256"), "hex")


def contactable_clause(channel: str) -> ColumnElement[bool]:
    if channel not in CHANNELS:
        raise ValueError("unknown channel")
    addr = contacts.c.email if channel == "email" else contacts.c.phone_e164
    suppressed = exists().where(
        suppressions.c.tenant_id == contacts.c.tenant_id,
        suppressions.c.channel.in_((channel, "all")),
        or_(
            suppressions.c.contact_id == contacts.c.id,
            suppressions.c.value_hash == _sha256_hex(addr),
            suppressions.c.value_hash == _sha256_hex(literal("contact:") + cast(contacts.c.id, String)),
        ),
    )
    latest_consent = (
        select(consent_records.c.status)
        .where(
            consent_records.c.tenant_id == contacts.c.tenant_id,
            consent_records.c.contact_id == contacts.c.id,
            consent_records.c.channel == channel,
        )
        .order_by(consent_records.c.captured_at.desc(), consent_records.c.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    return and_(
        contacts.c.status == "active",
        addr.is_not(None),
        ~suppressed,
        func.coalesce(latest_consent, "none") != "withdrawn",
    )
