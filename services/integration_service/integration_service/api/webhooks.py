"""Outbound webhook endpoints and delivery history. Managing endpoints needs `integrations:manage`; creating an
endpoint or changing its URL or secret is a credential change (step-up + audit). The signing secret is
supplied by the tenant, written to Vault and never returned."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from integration_service.api.models import (
    DeliveryDetail,
    DeliveryPage,
    EndpointCreate,
    EndpointOut,
    EndpointPage,
    EndpointPatch,
    SecretRotateIn,
)
from integration_service.deps import audit, db, register_tenant, settings_of
from integration_service.tables import secret_registry, webhook_deliveries, webhook_delivery_attempts, webhook_endpoints
from integration_service.webhooks import check_destination, expand_event_types
from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.errors import Conflict, NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from nexus_core.ssrf import UnsafeDestination
from nexus_core.vault import tenant_secret_path

router = APIRouter(prefix="/api/v1/webhooks/endpoints", tags=["webhooks"])
Read = Annotated[AuthContext, Depends(require_permission("integrations:read"))]
Manage = Annotated[AuthContext, Depends(require_permission("integrations:manage"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
E = webhook_endpoints


async def _validated_url(url: str) -> str:
    try:
        await check_destination(url)
    except UnsafeDestination as exc:
        # Only the rule that failed is reported, never resolved addresses.
        raise Unprocessable("The webhook URL is not allowed", extra={"reason": str(exc)}) from exc
    return url


async def _load(s: Any, endpoint_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
    stmt = select(E).where(E.c.id == endpoint_id, E.c.status != "deleted")
    if lock:
        stmt = stmt.with_for_update()
    row = (await s.execute(stmt)).mappings().first()
    if row is None:
        raise NotFound("Webhook endpoint not found")
    return dict(row)


async def _registry(s: Any, tenant_id: uuid.UUID, row: dict[str, Any], env: str) -> None:
    values = {"scope": "tenant", "tenant_id": tenant_id, "connection_id": row["id"], "owning_service":
              "integration-service", "provider": "webhooks", "kind": "webhook_signing", "environment": env,
              "status": "revoked" if row["status"] == "deleted" else row["status"],
              "vault_version": row["secret_version"]}  # fmt: skip
    await s.execute(
        insert(secret_registry)
        .values(vault_ref=row["secret_ref"], **values)
        .on_conflict_do_update(index_elements=["vault_ref"], set_={**values, "updated_at": func.now()})
    )


@router.get("", response_model=EndpointPage)
async def list_endpoints(request: Request, ctx: Read, limit: Limit = 50, cursor: Cursor = None) -> dict[str, Any]:
    stmt = apply_keyset(select(E).where(E.c.status != "deleted"), E.c.created_at, E.c.id, cursor, limit)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    return {"items": page, "next_cursor": next_cursor}


@router.post("", response_model=EndpointOut, status_code=201)
async def create_endpoint(body: EndpointCreate, request: Request, ctx: Manage, _: StepUp) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    cfg = settings_of(request)
    url = await _validated_url(body.url)
    events = expand_event_types(body.event_types)
    endpoint_id = uuid.uuid4()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        count = (await s.execute(select(func.count()).select_from(E).where(E.c.status != "deleted"))).scalar_one()
        if count >= cfg.webhook_max_endpoints_per_tenant:
            raise Conflict("The webhook endpoint limit for this workspace has been reached")
    ref = tenant_secret_path(cfg.env.value, str(tenant_id), "webhooks", str(endpoint_id))
    version = await request.app.state.secrets.write(ref, {"signing_secret": body.secret})
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await register_tenant(s, tenant_id)
        row = dict(
            (
                await s.execute(
                    insert(E)
                    .values(
                        id=endpoint_id,
                        tenant_id=tenant_id,
                        url=url,
                        description=body.description,
                        event_types=events,
                        secret_ref=ref,
                        secret_version=version,
                        created_by=str(ctx.actor)[:140],
                    )  # fmt: skip
                    .returning(E)
                )
            )
            .mappings()
            .one()
        )
        await _registry(s, tenant_id, row, cfg.env.value)
        await audit(s, ctx.actor, "webhook.endpoint.created", "webhook_endpoint", endpoint_id, tenant_id=tenant_id,
                    event_types=events)  # fmt: skip
    return row


@router.get("/{endpoint_id}", response_model=EndpointOut)
async def get_endpoint(endpoint_id: uuid.UUID, request: Request, ctx: Read) -> dict[str, Any]:
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        return await _load(s, endpoint_id)


@router.patch("/{endpoint_id}", response_model=EndpointOut)
async def patch_endpoint(
    endpoint_id: uuid.UUID, body: EndpointPatch, request: Request, ctx: Manage, _: StepUp
) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if not changes:
        raise Unprocessable("No changes requested")
    if "url" in changes:
        changes["url"] = await _validated_url(changes["url"])
    if "event_types" in changes:
        changes["event_types"] = expand_event_types(changes["event_types"])
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await _load(s, endpoint_id, lock=True)
        if current["version"] != body.expected_version:
            raise Conflict("The endpoint was changed by someone else; reload and retry")
        row = dict(
            (
                await s.execute(
                    update(E)
                    .where(E.c.id == endpoint_id)
                    .values(**changes, version=E.c.version + 1, updated_at=func.now())
                    .returning(E)
                )
            )
            .mappings()
            .one()
        )
        await _registry(s, tenant_id, row, settings_of(request).env.value)
        await audit(s, ctx.actor, "webhook.endpoint.updated", "webhook_endpoint", endpoint_id, tenant_id=tenant_id,
                    fields=sorted(changes))  # fmt: skip
    return row


@router.post("/{endpoint_id}/rotate-secret", response_model=EndpointOut)
async def rotate_secret(
    endpoint_id: uuid.UUID, body: SecretRotateIn, request: Request, ctx: Manage, _: StepUp
) -> dict[str, Any]:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        current = await _load(s, endpoint_id)
    version = await request.app.state.secrets.write(current["secret_ref"], {"signing_secret": body.secret})
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        row = dict(
            (
                await s.execute(
                    update(E)
                    .where(E.c.id == endpoint_id)
                    .values(secret_version=version, version=E.c.version + 1, updated_at=func.now())
                    .returning(E)
                )
            )
            .mappings()
            .one()
        )
        await s.execute(
            update(secret_registry)
            .where(secret_registry.c.vault_ref == current["secret_ref"])
            .values(vault_version=version, last_rotated_at=func.now(), updated_at=func.now())
        )
        await audit(s, ctx.actor, "webhook.endpoint.secret_rotated", "webhook_endpoint", endpoint_id,
                    tenant_id=tenant_id, vault_version=version)  # fmt: skip
    return row


@router.delete("/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: uuid.UUID, request: Request, ctx: Manage) -> Response:
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await _load(s, endpoint_id, lock=True)
        row = dict(
            (
                await s.execute(
                    update(E)
                    .where(E.c.id == endpoint_id)
                    .values(status="deleted", version=E.c.version + 1, updated_at=func.now())
                    .returning(E)
                )
            )
            .mappings()
            .one()
        )
        await s.execute(
            update(webhook_deliveries)
            .where(webhook_deliveries.c.endpoint_id == endpoint_id, webhook_deliveries.c.status == "queued")
            .values(status="canceled", updated_at=func.now())
        )
        await _registry(s, tenant_id, row, settings_of(request).env.value)
        await audit(s, ctx.actor, "webhook.endpoint.deleted", "webhook_endpoint", endpoint_id, tenant_id=tenant_id)
    await request.app.state.secrets.destroy(row["secret_ref"])
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------- deliveries
D = webhook_deliveries


@router.get("/{endpoint_id}/deliveries", response_model=DeliveryPage)
async def list_deliveries(endpoint_id: uuid.UUID, request: Request, ctx: Read, limit: Limit = 50,
                          cursor: Cursor = None, status: str | None = None) -> dict[str, Any]:  # fmt: skip
    stmt = select(D).where(D.c.endpoint_id == endpoint_id)
    if status:
        stmt = stmt.where(D.c.status == status)
    stmt = apply_keyset(stmt, D.c.created_at, D.c.id, cursor, limit)
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        await _load(s, endpoint_id)
        rows = [dict(r) for r in (await s.execute(stmt)).mappings().all()]
    page, next_cursor = build_page(rows, limit)
    return {"items": page, "next_cursor": next_cursor}


@router.get("/{endpoint_id}/deliveries/{delivery_id}", response_model=DeliveryDetail)
async def get_delivery(endpoint_id: uuid.UUID, delivery_id: uuid.UUID, request: Request, ctx: Read) -> dict[str, Any]:
    A = webhook_delivery_attempts  # noqa: N806
    async with db(request).tenant_session(ctx.require_tenant(), ctx.actor) as s:
        row = (
            (await s.execute(select(D).where(D.c.id == delivery_id, D.c.endpoint_id == endpoint_id))).mappings().first()
        )
        if row is None:
            raise NotFound("Delivery not found")
        attempts = (
            (await s.execute(select(A).where(A.c.delivery_id == delivery_id).order_by(A.c.attempt))).mappings().all()
        )
    return {**dict(row), "attempt_log": [dict(a) for a in attempts]}


@router.post("/{endpoint_id}/deliveries/{delivery_id}/redeliver", response_model=DeliveryDetail)
async def redeliver(endpoint_id: uuid.UUID, delivery_id: uuid.UUID, request: Request, ctx: Manage) -> dict[str, Any]:
    """Requeue a failed delivery (fresh attempt budget). Audited."""
    tenant_id = ctx.require_tenant()
    async with db(request).tenant_session(tenant_id, ctx.actor) as s:
        await _load(s, endpoint_id)
        row = (
            (
                await s.execute(
                    update(D)
                    .where(D.c.id == delivery_id, D.c.endpoint_id == endpoint_id, D.c.status == "failed")
                    .values(
                        status="queued", attempts=0, next_attempt_at=func.now(), enqueued_at=None, updated_at=func.now()
                    )  # fmt: skip
                    .returning(D)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("Only failed deliveries can be redelivered")
        await audit(s, ctx.actor, "webhook.delivery.redelivered", "webhook_delivery", delivery_id,
                    tenant_id=tenant_id)  # fmt: skip
    return {**dict(row), "attempt_log": []}
