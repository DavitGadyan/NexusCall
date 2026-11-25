"""Background work for the `imports.process` queue: CSV imports and lead exports, plus the stuck-job sweeper.

Task bodies are plain coroutines taking `deps` (anything with `.db` and `.store`, e.g. `TaskRuntime`), the
job context and string ids, so tests call them directly. Each job is claimed atomically
(`UPDATE ... WHERE status='queued' ... RETURNING`), so duplicate deliveries do nothing.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import available_timezones

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from crm_service.custom_fields import CustomFieldError, load_defs, validate_custom_fields
from crm_service.deps import audit, emit
from crm_service.domain import (
    add_consent,
    contact_updated,
    create_lead,
    find_by_addresses,
    insert_contact,
    update_contact,
)
from crm_service.normalize import InvalidAddress, normalize_email, normalize_phone
from crm_service.tables import contacts, crm_jobs, exports, import_rows, imports, leads
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.db import Database
from nexus_core.storage import ObjectStore, assert_tenant_key, tenant_key

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
_TZ = frozenset(available_timezones())
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
TEXT_LIMITS = {"first_name": 100, "last_name": 100, "company": 200, "source": 64}


class Deps(Protocol):
    @property
    def db(self) -> Database: ...

    @property
    def store(self) -> ObjectStore: ...


class RowError(ValueError):
    """Short, PII-free error code written to the import report."""


# --------------------------------------------------------------------------------------------- claim
async def _claim(session: AsyncSession, table: Table, job_id: uuid.UUID, stale_seconds: int) -> dict[str, Any] | None:
    stale = datetime.now(UTC) - timedelta(seconds=stale_seconds)
    row = (
        (
            await session.execute(
                update(table)
                .where(
                    table.c.id == job_id,
                    or_(table.c.status == "queued", (table.c.status == "processing") & (table.c.updated_at < stale)),
                )
                .values(status="processing", attempts=table.c.attempts + 1, updated_at=func.now())
                .returning(table)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row["attempts"] > MAX_ATTEMPTS:
        await _finish(session, table, job_id, status="failed", error_code="attempts_exhausted")
        return None
    await session.execute(
        update(crm_jobs).where(crm_jobs.c.job_id == job_id).values(status="processing", updated_at=func.now())
    )
    return dict(row)


async def _finish(session: AsyncSession, table: Table, job_id: uuid.UUID, *, status: str, **values: Any) -> None:
    await session.execute(
        update(table)
        .where(table.c.id == job_id)
        .values(status=status, completed_at=func.now(), updated_at=func.now(), **values)
    )
    await session.execute(
        update(crm_jobs).where(crm_jobs.c.job_id == job_id).values(status="done", updated_at=func.now())
    )


async def _heartbeat(session: AsyncSession, table: Table, job_id: uuid.UUID) -> None:
    await session.execute(update(table).where(table.c.id == job_id).values(updated_at=func.now()))
    await session.execute(update(crm_jobs).where(crm_jobs.c.job_id == job_id).values(updated_at=func.now()))


# -------------------------------------------------------------------------------------------- import
def _text(raw: dict[str, str | None], name: str) -> str | None:
    value = (raw.get(name) or "").strip()
    if not value:
        return None
    if _CONTROL.search(value) or len(value) > TEXT_LIMITS.get(name, 64):
        raise RowError(f"invalid_{name}")
    return value


def parse_row(raw: dict[str, str | None], default_region: str) -> dict[str, Any]:
    """Validate one CSV row into contact values (no database access)."""
    values: dict[str, Any] = {k: _text(raw, k) for k in TEXT_LIMITS}
    country = (raw.get("country") or "").strip().upper() or None
    if country is not None and not re.fullmatch(r"[A-Z]{2}", country):
        raise RowError("invalid_country")
    values["country"] = country
    tz = (raw.get("timezone") or "").strip() or None
    if tz is not None and tz not in _TZ:
        raise RowError("invalid_timezone")
    values["timezone"] = tz
    locale = (raw.get("locale") or "").strip() or None
    if locale is not None and not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", locale):
        raise RowError("invalid_locale")
    values["locale"] = locale
    email_raw = (raw.get("email") or "").strip()
    phone_raw = (raw.get("phone") or "").strip()
    try:
        values["email"] = normalize_email(email_raw) if email_raw else None
    except InvalidAddress as exc:
        raise RowError("invalid_email") from exc
    try:
        values["phone_e164"] = normalize_phone(phone_raw, country or default_region) if phone_raw else None
    except InvalidAddress as exc:
        raise RowError("invalid_phone") from exc
    if values["email"] is None and values["phone_e164"] is None:
        raise RowError("missing_email_or_phone")
    values["custom"] = {k[len("custom.") :]: v for k, v in raw.items() if k and k.startswith("custom.")}
    return values


async def import_one(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job: dict[str, Any],
    values: dict[str, Any],
    defs: dict[str, dict[str, Any]],
    ctx: JobContext,
) -> tuple[str, uuid.UUID]:
    """Dedupe by normalized email or phone: update the matching contact, or create contact + lead."""
    actor = ctx.actor
    by_email, by_phone = await find_by_addresses(session, values["email"], values["phone_e164"], lock=True)
    if by_email and by_phone and by_email["id"] != by_phone["id"]:
        raise RowError("email_and_phone_match_different_contacts")
    existing = by_email or by_phone
    if existing is not None and existing["status"] == "archived":
        raise RowError("contact_archived")
    custom_input = values.pop("custom")
    try:
        custom = validate_custom_fields(
            custom_input, defs, existing=existing["custom_fields"] if existing else None, from_text=True
        )
    except CustomFieldError as exc:
        raise RowError("invalid_custom_field") from exc
    provided = {k: v for k, v in values.items() if v is not None}
    if existing is None:
        provided.setdefault("source", job["default_source"])
        row = await insert_contact(
            session, tenant_id=tenant_id, values={**provided, "custom_fields": custom}, actor=actor
        )
        contact_id, status = row["id"], "created"
        await create_lead(
            session, tenant_id=tenant_id, contact_id=contact_id, actor=actor, values={"source": provided["source"]}
        )
    else:
        await update_contact(session, existing["id"], {**provided, "custom_fields": custom})
        contact_id, status = existing["id"], "updated"
    consent = job["consent"]
    if consent:
        for channel in consent["channels"]:
            if (channel == "email" and (values["email"] or (existing and existing["email"]))) or (
                channel == "phone" and (values["phone_e164"] or (existing and existing["phone_e164"]))
            ):
                await add_consent(
                    session,
                    tenant_id=tenant_id,
                    contact_id=contact_id,
                    channel=channel,
                    status=consent["status"],
                    lawful_basis=consent["lawful_basis"],
                    source=consent["source"],
                    source_ref=f"import:{job['id']}",
                    policy_version=consent.get("policy_version"),
                    evidence_ref=consent.get("evidence_ref"),
                    captured_at=datetime.fromisoformat(consent["captured_at"]) if consent.get("captured_at") else None,
                    actor=actor,
                )
    await contact_updated(session, tenant_id=tenant_id, actor=actor, contact_id=contact_id)
    return status, contact_id


def _read_csv(data: bytes) -> list[dict[str, str | None]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    return [{k: v for k, v in r.items() if k is not None and not isinstance(v, list)} for r in reader]


async def process_import(
    deps: Deps,
    ctx: JobContext,
    import_id: uuid.UUID,
    *,
    chunk_rows: int = 500,
    max_rows: int = 50_000,
    max_bytes: int = 10 * 1024 * 1024,
    stale_seconds: int = 600,
) -> str:
    tenant_id, actor = ctx.tenant_id, ctx.actor
    async with deps.db.tenant_session(tenant_id, actor) as s:
        job = await _claim(s, imports, import_id, stale_seconds)
    if job is None:
        return "skipped"
    try:
        data = await deps.store.get("imports", assert_tenant_key(tenant_id, job["object_key"]), max_bytes=max_bytes)
    except Exception as exc:
        raise TransientFailure("import file unavailable") from exc
    try:
        rows = _read_csv(data)
    except (UnicodeDecodeError, csv.Error):
        async with deps.db.tenant_session(tenant_id, actor) as s:
            await _finish(s, imports, import_id, status="failed", error_code="invalid_csv")
        return "failed"
    if len(rows) > max_rows:
        async with deps.db.tenant_session(tenant_id, actor) as s:
            await _finish(s, imports, import_id, status="failed", error_code="too_many_rows", total_rows=len(rows))
        return "failed"
    async with deps.db.tenant_session(tenant_id, actor) as s:
        done = set(
            (await s.execute(select(import_rows.c.row_number).where(import_rows.c.import_id == import_id))).scalars()
        )
    for start in range(0, len(rows), chunk_rows):
        async with deps.db.tenant_session(tenant_id, actor) as s:
            defs = await load_defs(s)
            for offset, raw in enumerate(rows[start : start + chunk_rows]):
                number = start + offset + 1
                if number in done:
                    continue  # resumed after a crash: rows already recorded are not applied twice
                status, contact_id, error = "failed", None, None
                try:
                    async with s.begin_nested():
                        status, contact_id = await import_one(
                            s,
                            tenant_id=tenant_id,
                            job=job,
                            values=parse_row(raw, job["default_region"]),
                            defs=defs,
                            ctx=ctx,
                        )
                except RowError as exc:
                    error = str(exc)
                except IntegrityError:
                    error = "duplicate_address"
                await s.execute(
                    insert(import_rows)
                    .values(
                        tenant_id=tenant_id,
                        import_id=import_id,
                        row_number=number,
                        status=status,
                        contact_id=contact_id,
                        error=error,
                    )
                    .on_conflict_do_nothing(constraint="uq_import_rows_row")
                )
            await _heartbeat(s, imports, import_id)
    async with deps.db.tenant_session(tenant_id, actor) as s:
        results = (
            await s.execute(
                select(import_rows.c.row_number, import_rows.c.status, import_rows.c.error)
                .where(import_rows.c.import_id == import_id)
                .order_by(import_rows.c.row_number)
            )
        ).all()
        counts = {"created": 0, "updated": 0, "failed": 0}
        report = io.StringIO()
        writer = csv.writer(report)
        writer.writerow(["row", "status", "error"])
        for r in results:
            counts[r.status] += 1
            writer.writerow([r.row_number, r.status, r.error or ""])
        report_key = tenant_key(tenant_id, "imports", f"{import_id}.report.csv")
        await deps.store.put("imports", report_key, report.getvalue().encode(), content_type="text/csv")
        await _finish(
            s,
            imports,
            import_id,
            status="completed",
            total_rows=len(results),
            created_count=counts["created"],
            updated_count=counts["updated"],
            failed_count=counts["failed"],
            report_key=report_key,
        )
        await emit(
            s,
            "import.completed",
            tenant_id=tenant_id,
            actor=actor,
            payload={"import_id": str(import_id), "total": len(results), **counts},
        )
        await audit(s, actor, "import.completed", "import", import_id, tenant_id=tenant_id, **counts)
    return "completed"


# -------------------------------------------------------------------------------------------- export
EXPORT_COLUMNS = (
    "lead_id",
    "status",
    "source",
    "score",
    "qualified",
    "contact_id",
    "first_name",
    "last_name",
    "company",
    "email",
    "phone_e164",
    "created_at",
)


def safe_cell(value: Any) -> str:
    """Neutralize spreadsheet formula injection (cells starting with = + - @ tab or CR)."""
    if value is None:
        return ""
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def build_export(
    deps: Deps, ctx: JobContext, export_id: uuid.UUID, *, max_rows: int = 100_000, stale_seconds: int = 600
) -> str:
    tenant_id, actor = ctx.tenant_id, ctx.actor
    async with deps.db.tenant_session(tenant_id, actor) as s:
        job = await _claim(s, exports, export_id, stale_seconds)
        if job is None:
            return "skipped"
        f = job["filters"] or {}
        stmt = (
            select(
                leads.c.id.label("lead_id"),
                leads.c.status,
                leads.c.source,
                leads.c.score,
                leads.c.qualified,
                leads.c.contact_id,
                contacts.c.first_name,
                contacts.c.last_name,
                contacts.c.company,
                contacts.c.email,
                contacts.c.phone_e164,
                leads.c.created_at,
            )
            .select_from(leads.join(contacts, contacts.c.id == leads.c.contact_id))
            .where(contacts.c.status != "archived")
            .order_by(leads.c.created_at, leads.c.id)
            .limit(max_rows)
        )
        if f.get("status"):
            stmt = stmt.where(leads.c.status.in_(f["status"]))
        for name in ("source", "campaign_id", "owner_id"):
            if f.get(name):
                stmt = stmt.where(leads.c[name] == f[name])
        if f.get("created_from"):
            stmt = stmt.where(leads.c.created_at >= datetime.fromisoformat(f["created_from"]))
        if f.get("created_to"):
            stmt = stmt.where(leads.c.created_at < datetime.fromisoformat(f["created_to"]))
        rows = (await s.execute(stmt)).mappings().all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow([safe_cell(r[c]) for c in EXPORT_COLUMNS])
    key = tenant_key(tenant_id, "exports", f"{export_id}.csv")
    try:
        await deps.store.put("exports", key, out.getvalue().encode(), content_type="text/csv")
    except Exception as exc:
        raise TransientFailure("export storage unavailable") from exc
    async with deps.db.tenant_session(tenant_id, actor) as s:
        await _finish(s, exports, export_id, status="completed", object_key=key, row_count=len(rows))
        await audit(s, actor, "leads.export.completed", "export", export_id, tenant_id=tenant_id, rows=len(rows))
    return "completed"


# ------------------------------------------------------------------------------------------- sweeper
TASKS = {"import": ("imports.process", "import_id"), "export": ("exports.build", "export_id")}
Enqueue = Callable[..., Awaitable[None]]


async def sweep_stuck_jobs(db: Database, enqueue: Enqueue, *, stale_seconds: int = 600, batch: int = 100) -> int:
    """Re-enqueue jobs left queued/processing too long (lost publish, crashed worker). Idempotent."""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_seconds)
    async with db.global_session() as s:
        rows = (
            await s.execute(
                update(crm_jobs)
                .where(
                    crm_jobs.c.job_id.in_(
                        select(crm_jobs.c.job_id)
                        .where(crm_jobs.c.status != "done", crm_jobs.c.updated_at < cutoff)
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                )
                .values(updated_at=func.now())
                .returning(crm_jobs.c.job_id, crm_jobs.c.tenant_id, crm_jobs.c.kind)
            )
        ).all()
    for r in rows:
        name, arg = TASKS[r.kind]
        ctx = JobContext(
            tenant_id=r.tenant_id, correlation_id=uuid.uuid4(), actor_type="system", actor_id="crm-sweeper"
        )
        try:
            await enqueue(name, "imports.process", ctx, **{arg: str(r.job_id)})
        except Exception:  # noqa: BLE001 - tried again on the next sweep
            log.warning("sweeper_enqueue_failed", extra={"job_id": str(r.job_id)})
    return len(rows)
