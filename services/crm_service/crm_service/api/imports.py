"""CSV imports: upload (≤10 MB) → `imports` bucket → `imports.process` task → per-row report."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from crm_service import idem
from crm_service.deps import audit, db, enqueue_job, job_context, store
from crm_service.schemas import In, Items, LawfulBasis, clean_text
from crm_service.tables import crm_jobs, imports
from nexus_core.authz import AuthContext, require_permission
from nexus_core.errors import NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.storage import assert_tenant_key, tenant_key

router = APIRouter(prefix="/api/v1/imports", tags=["imports"])
Create = Annotated[AuthContext, Depends(require_permission("leads:create"))]

STANDARD_COLUMNS = frozenset(
    {"first_name", "last_name", "company", "email", "phone", "timezone", "country", "locale", "source"}
)
TASK = "imports.process"


class ImportConsent(In):
    channels: list[Literal["phone", "email"]] = Field(min_length=1, max_length=2)
    status: Literal["granted", "withdrawn"] = "granted"
    lawful_basis: LawfulBasis = "consent"
    source: str = Field(min_length=1, max_length=64)
    policy_version: str | None = Field(default=None, max_length=32)
    captured_at: datetime | None = None
    evidence_ref: str | None = Field(default=None, max_length=255)

    @field_validator("source", "policy_version", "evidence_ref")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v)


class ImportOptions(In):
    default_region: str = Field(default="US", pattern=r"^[A-Z]{2}$")
    source: str | None = Field(default="import", max_length=64)
    consent: ImportConsent | None = None


class ImportOut(BaseModel):
    id: uuid.UUID
    status: str
    filename: str
    size_bytes: int
    total_rows: int
    created_count: int
    updated_count: int
    failed_count: int
    error_code: str | None
    has_report: bool
    created_at: datetime
    completed_at: datetime | None


class ReportLink(BaseModel):
    url: str
    expires_in: int


def to_out(row: Any) -> ImportOut:
    return ImportOut(
        **{k: row[k] for k in ImportOut.model_fields if k != "has_report"}, has_report=row["report_key"] is not None
    )


def check_header(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Unprocessable("The file must be UTF-8 encoded CSV") from exc
    try:
        header = next(csv.reader(io.StringIO(text)), [])
    except csv.Error as exc:
        raise Unprocessable("The file is not valid CSV") from exc
    columns = [h.strip().lower() for h in header]
    if not columns or len(columns) > 100 or len(set(columns)) != len(columns):
        raise Unprocessable("The CSV header must have 1..100 unique columns")
    unknown = sorted(c for c in columns if c not in STANDARD_COLUMNS and not c.startswith("custom."))
    if unknown:
        raise Unprocessable("Unknown CSV columns", extra={"columns": unknown[:20]})
    if "email" not in columns and "phone" not in columns:
        raise Unprocessable("The CSV needs an email or a phone column")
    return columns


@router.post("", status_code=202, response_model=ImportOut)
async def create_import(
    request: Request,
    ctx: Create,
    file: Annotated[UploadFile, File(description="CSV, UTF-8, at most 10 MB")],
    options: Annotated[str, Form(max_length=4000)] = "{}",
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    settings = request.app.state.settings
    tenant_id = ctx.require_tenant()
    key = idem.require_key(idempotency_key)
    try:
        opts = ImportOptions.model_validate(json.loads(options))
    except (ValueError, ValidationError) as exc:
        raise Unprocessable("Invalid import options") from exc
    filename = (file.filename or "import.csv").rsplit("/", 1)[-1][:255]
    if not filename.lower().endswith(".csv"):
        raise Unprocessable("Only .csv files can be imported")
    data = await file.read(settings.import_max_bytes + 1)
    if len(data) > settings.import_max_bytes:
        raise Unprocessable("The file exceeds the 10 MB import limit")
    if not data:
        raise Unprocessable("The file is empty")
    check_header(data)
    import_id = uuid.uuid4()
    object_key = tenant_key(tenant_id, "imports", f"{import_id}.csv")
    payload = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "filename": filename,
        "options": opts.model_dump(mode="json"),
    }
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        op = await idem.begin(s, tenant_id=tenant_id, operation="imports.create", key=key, payload=payload)
        if op.replay is not None:
            return op.replay
        await store(request).put("imports", object_key, data, content_type="text/csv")
        row = (
            (
                await s.execute(
                    insert(imports)
                    .values(
                        id=import_id,
                        tenant_id=tenant_id,
                        filename=filename,
                        object_key=object_key,
                        size_bytes=len(data),
                        default_region=opts.default_region,
                        default_source=opts.source,
                        consent=opts.consent.model_dump(mode="json") if opts.consent else None,
                        created_by=str(ctx.actor),
                    )
                    .returning(imports)
                )
            )
            .mappings()
            .one()
        )
        await s.execute(insert(crm_jobs).values(job_id=import_id, tenant_id=tenant_id, kind="import", status="queued"))
        await audit(s, ctx.actor, "import.created", "import", import_id, tenant_id=tenant_id, size_bytes=len(data))
        body = await op.done(202, to_out(row).model_dump(mode="json"), import_id)
    await enqueue_job(request, TASK, job_context(tenant_id, ctx.actor), import_id=str(import_id))
    return body


@router.get("", response_model=Items[ImportOut])
async def list_imports(request: Request, ctx: Create, limit: Limit = 50, cursor: Cursor = None) -> Items[ImportOut]:
    stmt = apply_keyset(select(imports), imports.c.created_at, imports.c.id, cursor, limit)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = list((await s.execute(stmt)).mappings().all())
    page, next_cursor = build_page(rows, limit)
    return Items[ImportOut](items=[to_out(r) for r in page], next_cursor=next_cursor)


async def _load(request: Request, ctx: AuthContext, import_id: uuid.UUID) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = (await s.execute(select(imports).where(imports.c.id == import_id))).mappings().first()
    if row is None:
        raise NotFound("Import not found")
    return dict(row)


@router.get("/{import_id}", response_model=ImportOut)
async def get_import(import_id: uuid.UUID, request: Request, ctx: Create) -> ImportOut:
    return to_out(await _load(request, ctx, import_id))


@router.get("/{import_id}/report", response_model=ReportLink)
async def report_link(import_id: uuid.UUID, request: Request, ctx: Create) -> ReportLink:
    tenant_id = ctx.require_tenant()
    row = await _load(request, ctx, import_id)
    if row["report_key"] is None:
        raise NotFound("The report is not ready yet")
    seconds = int(request.app.state.settings.presign_seconds)
    url = await store(request).presign_get(
        "imports",
        assert_tenant_key(tenant_id, row["report_key"]),
        expires_seconds=seconds,
        download_filename=f"import-{import_id}-report.csv",
    )
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await audit(s, ctx.actor, "import.report.downloaded", "import", import_id, tenant_id=tenant_id)
    return ReportLink(url=url, expires_in=seconds)
