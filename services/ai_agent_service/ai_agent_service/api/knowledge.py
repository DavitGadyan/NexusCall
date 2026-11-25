"""Knowledge sources, document upload and review, retrieval test, re-embedding (OWASP LLM05 / LLM09)."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Header, Request, UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_agent_service import domain, idem
from ai_agent_service.deps import audit, job_context, publish, register_job
from ai_agent_service.gateway import AppDeps, tenant_ai_policy
from ai_agent_service.knowledge import (
    KNOWLEDGE_BUCKET,
    delete_document_rows,
    enforce_search_rate,
    purge_object,
    search,
    stale_chunk_count,
    tombstone,
)
from ai_agent_service.schemas import (
    DeleteOut,
    DocumentOut,
    DocumentPage,
    ReembedOut,
    ReviewIn,
    SearchIn,
    SearchOut,
    SourceCreate,
    SourceOut,
    SourcePage,
    SourcePatch,
)
from ai_agent_service.tables import knowledge_chunks, knowledge_documents, knowledge_sources
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.storage import tenant_key

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ai/knowledge", tags=["knowledge"])
Reader = Annotated[AuthContext, Depends(require_permission("ai:agents:read"))]
Manager = Annotated[AuthContext, Depends(require_permission("ai:agents:manage"))]
s, d, c = knowledge_sources.c, knowledge_documents.c, knowledge_chunks.c
_step_up = require_step_up()


async def _get_source(session: AsyncSession, tenant_id: uuid.UUID, source_id: uuid.UUID, *, lock: bool = False) -> Any:
    stmt = select(knowledge_sources).where(s.tenant_id == tenant_id, s.id == source_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Knowledge source not found")
    return row


async def _get_document(
    session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
) -> Any:
    stmt = select(knowledge_documents).where(d.tenant_id == tenant_id, d.id == document_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Document not found")
    return row


# ------------------------------------------------------------------------------------------- sources
@router.get("/sources", response_model=SourcePage)
async def list_sources(request: Request, ctx: Reader, limit: Limit = 50, cursor: Cursor = None) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        stmt = select(knowledge_sources).where(s.tenant_id == tenant_id)
        rows = (await session.execute(apply_keyset(stmt, s.created_at, s.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.post("/sources", response_model=SourceOut, status_code=201)
async def create_source(
    request: Request, body: SourceCreate, ctx: Manager, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    tenant_id = ctx.require_tenant()
    key = idem.require_key(idempotency_key)
    requires_approval = domain.source_requires_approval(body.origin, body.classification, body.requires_approval)
    try:
        async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
            i = await idem.begin(
                session, tenant_id=tenant_id, operation="knowledge.sources.create", key=key, payload=body.model_dump()
            )
            if i.replay is not None:
                return i.replay
            row = (
                (
                    await session.execute(
                        knowledge_sources.insert()
                        .values(
                            tenant_id=tenant_id,
                            **body.model_dump(exclude={"requires_approval"}),
                            requires_approval=requires_approval,
                            created_by=str(ctx.actor)[:160],
                        )
                        .returning(knowledge_sources)
                    )
                )
                .mappings()
                .one()
            )
            await audit(
                session,
                ctx.actor,
                "knowledge.source.created",
                "knowledge_source",
                row["id"],
                tenant_id=tenant_id,
                classification=body.classification,
                requires_approval=requires_approval,
            )
            out = SourceOut.model_validate(dict(row))
            return await i.done(201, out.model_dump(mode="json"), row["id"])
    except IntegrityError as exc:
        raise Conflict("A knowledge source with this name already exists") from exc


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source(request: Request, source_id: uuid.UUID, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        return await _get_source(session, tenant_id, source_id)


@router.patch("/sources/{source_id}", response_model=SourceOut)
async def patch_source(request: Request, source_id: uuid.UUID, body: SourcePatch, ctx: Manager) -> Any:
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        row = await _get_source(session, tenant_id, source_id, lock=True)
        if row["version"] != body.version:
            raise Conflict("The source was changed by someone else; reload and retry")
        merged = {**row, **changes}
        # The approval requirement is recomputed server-side: a client can tighten it, never loosen it below
        # what origin and classification demand.
        changes["requires_approval"] = domain.source_requires_approval(
            merged["origin"], merged["classification"], bool(merged["requires_approval"])
        )
        try:
            updated = (
                (
                    await session.execute(
                        update(knowledge_sources)
                        .where(s.tenant_id == tenant_id, s.id == source_id)
                        .values(**changes, version=s.version + 1, updated_at=func.now())
                        .returning(knowledge_sources)
                    )
                )
                .mappings()
                .one()
            )
        except IntegrityError as exc:
            raise Conflict("A knowledge source with this name already exists") from exc
        if changes["requires_approval"] and not row["requires_approval"]:
            # Newly high-risk: already indexed documents need approval before they are retrievable again.
            await session.execute(
                update(knowledge_documents)
                .where(d.tenant_id == tenant_id, d.source_id == source_id, d.review_state == "not_required")
                .values(review_state="pending_approval", updated_at=func.now())
            )
        await audit(
            session,
            ctx.actor,
            "knowledge.source.updated",
            "knowledge_source",
            source_id,
            tenant_id=tenant_id,
            fields=sorted(changes),
        )
        return updated


@router.delete("/sources/{source_id}", response_model=DeleteOut)
async def delete_source(request: Request, source_id: uuid.UUID, ctx: Manager) -> Any:
    """Deletes the source, its documents, chunks and embeddings in ONE transaction, and tombstones the stored
    files so they are removed even if object storage is briefly unavailable (LLM09 lifecycle)."""
    tenant_id = ctx.require_tenant()
    deps = AppDeps(request.app)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        await _get_source(session, tenant_id, source_id, lock=True)
        docs = (
            await session.execute(
                select(d.id, d.object_key).where(d.tenant_id == tenant_id, d.source_id == source_id).with_for_update()
            )
        ).all()
        chunks = 0
        tombstones = []
        for doc in docs:
            chunks += await delete_document_rows(session, tenant_id, doc.id)
            if (t := await tombstone(session, tenant_id, doc.object_key)) is not None:
                tombstones.append(t)
        await session.execute(knowledge_sources.delete().where(s.tenant_id == tenant_id, s.id == source_id))
        await audit(
            session,
            ctx.actor,
            "knowledge.source.deleted",
            "knowledge_source",
            source_id,
            tenant_id=tenant_id,
            documents=len(docs),
            chunks=chunks,
        )
    for t in tombstones:
        await purge_object(deps, tenant_id, t)
    return {"deleted": True, "chunks_deleted": chunks, "documents_deleted": len(docs)}


# ------------------------------------------------------------------------------------------- documents
@router.get("/sources/{source_id}/documents", response_model=DocumentPage)
async def list_documents(
    request: Request, source_id: uuid.UUID, ctx: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        await _get_source(session, tenant_id, source_id)
        stmt = select(knowledge_documents).where(d.tenant_id == tenant_id, d.source_id == source_id)
        rows = (await session.execute(apply_keyset(stmt, d.created_at, d.id, cursor, limit))).mappings().all()
    items, nxt = build_page(list(rows), limit)
    return {"items": items, "next_cursor": nxt}


@router.post("/sources/{source_id}/documents", response_model=DocumentOut, status_code=202)
async def upload_document(
    request: Request,
    source_id: uuid.UUID,
    ctx: Manager,
    file: Annotated[UploadFile, File(description="txt, md or pdf; at most 10 MB")],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    """Step 1 of the ingestion pipeline: type/size validation (extension AND content), provenance (uploaded_by,
    sha256, version) and storage. Scanning, extraction, PII flagging, content analysis, approval and indexing
    run in the `ai.knowledge_ingest` task. `approved` is never accepted from the client."""
    tenant_id = ctx.require_tenant()
    key = idem.require_key(idempotency_key)
    settings = request.app.state.settings
    data = await file.read(settings.knowledge_max_upload_bytes + 1)
    filename = domain.safe_filename(file.filename or "document")
    try:
        content_type = domain.validate_upload(filename, data, max_bytes=settings.knowledge_max_upload_bytes)
    except domain.FileRejected as exc:
        raise Unprocessable("The file was rejected", extra={"code_detail": exc.code}) from exc
    digest = domain.sha256_hex(data)
    deps = AppDeps(request.app)
    document_id = uuid.uuid4()
    object_key = tenant_key(tenant_id, "sources", str(source_id), f"{document_id}.bin")
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        i = await idem.begin(
            session,
            tenant_id=tenant_id,
            operation=f"knowledge.documents.upload:{source_id}",
            key=key,
            payload={"sha256": digest, "filename": filename},
        )
        if i.replay is not None:
            return i.replay
        source = await _get_source(session, tenant_id, source_id, lock=True)
        if source["status"] != "active":
            raise Conflict("The knowledge source is disabled")
        duplicate = (
            await session.execute(
                select(d.id).where(d.tenant_id == tenant_id, d.source_id == source_id, d.sha256 == digest)
            )
        ).first()
        if duplicate is not None:
            raise Conflict("This file is already in the source", extra={"document_id": str(duplicate.id)})
        doc_version = (
            await session.execute(
                select(func.coalesce(func.max(d.doc_version), 0) + 1).where(
                    d.tenant_id == tenant_id, d.source_id == source_id, d.filename == filename
                )
            )
        ).scalar_one()
        await deps.store.put(KNOWLEDGE_BUCKET, object_key, data, content_type=content_type)
        row = (
            (
                await session.execute(
                    knowledge_documents.insert()
                    .values(
                        id=document_id,
                        tenant_id=tenant_id,
                        source_id=source_id,
                        filename=filename,
                        content_type=content_type,
                        size_bytes=len(data),
                        sha256=digest,
                        object_key=object_key,
                        doc_version=doc_version,
                        uploaded_by=str(ctx.actor)[:160],
                        status="queued",
                    )
                    .returning(knowledge_documents)
                )
            )
            .mappings()
            .one()
        )
        await register_job(session, document_id, tenant_id, "ingest")
        await audit(
            session,
            ctx.actor,
            "knowledge.document.uploaded",
            "knowledge_document",
            document_id,
            tenant_id=tenant_id,
            source_id=str(source_id),
            sha256=digest,
            size_bytes=len(data),
            content_type=content_type,
        )
        out = DocumentOut.model_validate(dict(row))
        body = await i.done(202, out.model_dump(mode="json"), document_id)
    await publish(request.app, "ingest", job_context(tenant_id, ctx.actor), document_id)
    return body


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(request: Request, document_id: uuid.UUID, ctx: Reader) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        return await _get_document(session, tenant_id, document_id)


@router.delete("/documents/{document_id}", response_model=DeleteOut)
async def delete_document(request: Request, document_id: uuid.UUID, ctx: Manager) -> Any:
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        doc = await _get_document(session, tenant_id, document_id, lock=True)
        chunks = await delete_document_rows(session, tenant_id, document_id)
        tid = await tombstone(session, tenant_id, doc["object_key"])
        await audit(
            session,
            ctx.actor,
            "knowledge.document.deleted",
            "knowledge_document",
            document_id,
            tenant_id=tenant_id,
            chunks=chunks,
        )
    if tid is not None:
        await purge_object(AppDeps(request.app), tenant_id, tid)
    return {"deleted": True, "chunks_deleted": chunks, "documents_deleted": 1}


@router.post("/documents/{document_id}/review", response_model=DocumentOut)
async def review_document(
    request: Request,
    document_id: uuid.UUID,
    body: ReviewIn,
    ctx: Manager,
    _: AuthContext = Depends(_step_up),
) -> Any:
    """The ONLY way a flagged or high-risk document becomes retrievable: a human decision with step-up MFA,
    audited (LLM05). Rejection removes its chunks and embeddings."""
    tenant_id = ctx.require_tenant()
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        doc = await _get_document(session, tenant_id, document_id, lock=True)
        if doc["status"] != "indexed" or doc["review_state"] not in ("pending_approval", "needs_review"):
            raise Conflict("This document is not awaiting review")
        state = "approved" if body.decision == "approve" else "rejected"
        if state == "rejected":
            await session.execute(
                knowledge_chunks.delete().where(c.tenant_id == tenant_id, c.document_id == document_id)
            )
        updated = (
            (
                await session.execute(
                    update(knowledge_documents)
                    .where(d.tenant_id == tenant_id, d.id == document_id)
                    .values(
                        review_state=state,
                        reviewed_by=str(ctx.actor)[:160],
                        reviewed_at=func.now(),
                        updated_at=func.now(),
                        chunk_count=0 if state == "rejected" else d.chunk_count,
                    )
                    .returning(knowledge_documents)
                )
            )
            .mappings()
            .one()
        )
        await audit(
            session,
            ctx.actor,
            f"knowledge.document.{state}",
            "knowledge_document",
            document_id,
            tenant_id=tenant_id,
            previous_state=doc["review_state"],
            review_reasons=list(doc["review_reasons"]),
        )
        return updated


# ------------------------------------------------------------------------------------------- retrieval test
@router.post("/search", response_model=SearchOut)
async def search_test(request: Request, body: SearchIn, ctx: Reader) -> Any:
    """Retrieval test for knowledge managers. Rate-limited; raw similarity only for `ai:agents:manage`."""
    tenant_id = ctx.require_tenant()
    deps = AppDeps(request.app)
    await enforce_search_rate(deps, tenant_id, ctx.actor, per_user=True)
    policy = await tenant_ai_policy(deps, tenant_id)
    hits, model = await search(
        deps,
        tenant_id=tenant_id,
        actor=ctx.actor,
        query=body.query,
        top_k=body.top_k,
        cap=str(policy.get("data_classification_max", "internal")),
        roles=ctx.roles,
        source_ids=body.source_ids,
    )
    show_scores = ctx.can("ai:agents:manage")
    return {
        "embedding_model": model,
        "results": [
            {
                "rank": h.rank,
                "chunk_id": h.chunk_id,
                "source_id": h.source_id,
                "source_name": h.source_name,
                "document_id": h.document_id,
                "text": h.text,
                "similarity": h.similarity if show_scores else None,
            }
            for h in hits
        ],
    }


@router.post("/reembed", response_model=ReembedOut, status_code=202)
async def reembed(request: Request, ctx: Manager) -> Any:
    """Re-embed every chunk that is not on the configured embedding model (after a model change)."""
    tenant_id = ctx.require_tenant()
    model = request.app.state.settings.embedding_model
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
        stale = await stale_chunk_count(session, tenant_id, model)
        job_id = uuid.uuid4() if stale else None
        if job_id is not None:
            await register_job(session, job_id, tenant_id, "reembed")
            await audit(
                session,
                ctx.actor,
                "knowledge.reembed_requested",
                "knowledge_base",
                None,
                tenant_id=tenant_id,
                stale_chunks=stale,
                embedding_model=model,
            )
    if job_id is not None:
        await publish(request.app, "reembed", job_context(tenant_id, ctx.actor), job_id)
    return {"job_id": job_id, "stale_chunks": stale, "embedding_model": model}
