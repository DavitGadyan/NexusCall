"""Public notification API for the header bell, the notification list and preferences.

Any member of the tenant may call these; each caller sees only notifications addressed to them or to a
permission they currently hold (resolved server-side by tenant-service on every request)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.authz import AuthContext, AuthDep
from nexus_core.errors import Conflict, Forbidden, NotFound, Unprocessable
from nexus_core.pagination import Cursor, Limit, apply_keyset, build_page
from notification_service.domain import KIND_RE, visible_to
from notification_service.tables import notification_preferences, notification_reads, notifications

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])
UNREAD_COUNT_CAP = 1000


class NotificationOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    link: str | None
    kind: str
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_cursor: str | None
    unread_count: int


class ReadOut(BaseModel):
    id: uuid.UUID
    read_at: datetime


class ReadAllOut(BaseModel):
    marked: int


class PreferencesOut(BaseModel):
    email_enabled: bool
    muted_kinds: list[str]
    version: int


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_enabled: bool = True
    muted_kinds: list[str] = Field(default_factory=list, max_length=20)
    expected_version: int | None = None


def viewer_of(ctx: AuthContext) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = ctx.require_tenant()
    if ctx.principal.is_service:
        raise Forbidden("Notifications are for human users")
    try:
        return tenant_id, uuid.UUID(ctx.principal.sub)
    except ValueError as exc:
        raise Forbidden("Notifications are for human users") from exc


async def load_preferences(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                select(
                    notification_preferences.c.email_enabled,
                    notification_preferences.c.muted_kinds,
                    notification_preferences.c.version,
                ).where(notification_preferences.c.user_id == user_id)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"email_enabled": True, "muted_kinds": [], "version": 0}
    return {"email_enabled": row["email_enabled"], "muted_kinds": list(row["muted_kinds"]), "version": row["version"]}


def _scope(ctx: AuthContext, user_id: uuid.UUID, muted: list[str]) -> Any:
    clause = visible_to(user_id, ctx.permissions)
    if muted:
        clause = and_(clause, notifications.c.kind.notin_(muted))
    return clause


def _read_join(user_id: uuid.UUID) -> Any:
    return notifications.outerjoin(
        notification_reads,
        and_(
            notification_reads.c.tenant_id == notifications.c.tenant_id,
            notification_reads.c.notification_id == notifications.c.id,
            notification_reads.c.user_id == user_id,
        ),
    )


@router.get("", response_model=NotificationPage)
async def list_notifications(
    request: Request, ctx: AuthDep, unread: bool = False, limit: Limit = 50, cursor: Cursor = None
) -> dict[str, Any]:
    tenant_id, user_id = viewer_of(ctx)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        prefs = await load_preferences(s, user_id)
        scope = _scope(ctx, user_id, prefs["muted_kinds"])
        stmt = (
            select(
                notifications.c.id,
                notifications.c.title,
                notifications.c.body,
                notifications.c.link,
                notifications.c.kind,
                notifications.c.created_at,
                notification_reads.c.read_at,
            )
            .select_from(_read_join(user_id))
            .where(scope)
        )
        if unread:
            stmt = stmt.where(notification_reads.c.read_at.is_(None))
        rows = (
            (await s.execute(apply_keyset(stmt, notifications.c.created_at, notifications.c.id, cursor, limit)))
            .mappings()
            .all()
        )
        unread_q = (
            select(literal(1))
            .select_from(_read_join(user_id))
            .where(scope, notification_reads.c.read_at.is_(None))
            .limit(UNREAD_COUNT_CAP)
            .subquery()
        )
        unread_count = (await s.execute(select(func.count()).select_from(unread_q))).scalar_one()
    page, next_cursor = build_page(list(rows), limit)
    return {"items": [dict(r) for r in page], "next_cursor": next_cursor, "unread_count": unread_count}


@router.post("/read-all", response_model=ReadAllOut)
async def read_all(request: Request, ctx: AuthDep) -> dict[str, Any]:
    tenant_id, user_id = viewer_of(ctx)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        unread = (
            select(notifications.c.tenant_id, notifications.c.id, literal(user_id))
            .select_from(_read_join(user_id))
            .where(visible_to(user_id, ctx.permissions), notification_reads.c.read_at.is_(None))
        )
        result = await s.execute(
            insert(notification_reads)
            .from_select(["tenant_id", "notification_id", "user_id"], unread)
            .on_conflict_do_nothing()
            .returning(notification_reads.c.notification_id)
        )
        marked = len(result.all())
    return {"marked": marked}


@router.post("/{notification_id}/read", response_model=ReadOut)
async def mark_read(notification_id: uuid.UUID, request: Request, ctx: AuthDep) -> dict[str, Any]:
    tenant_id, user_id = viewer_of(ctx)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        visible = (
            await s.execute(
                select(notifications.c.id).where(
                    notifications.c.id == notification_id, visible_to(user_id, ctx.permissions)
                )
            )
        ).first()
        if visible is None:  # another tenant's, another user's, or a permission the caller does not hold
            raise NotFound("Notification not found")
        await s.execute(
            insert(notification_reads)
            .values(tenant_id=tenant_id, notification_id=notification_id, user_id=user_id)
            .on_conflict_do_nothing()
        )
        read_at = (
            await s.execute(
                select(notification_reads.c.read_at).where(
                    notification_reads.c.notification_id == notification_id, notification_reads.c.user_id == user_id
                )
            )
        ).scalar_one()
    return {"id": notification_id, "read_at": read_at}


@router.get("/preferences", response_model=PreferencesOut)
async def get_preferences(request: Request, ctx: AuthDep) -> dict[str, Any]:
    tenant_id, user_id = viewer_of(ctx)
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        return await load_preferences(s, user_id)


@router.put("/preferences", response_model=PreferencesOut)
async def put_preferences(body: PreferencesIn, request: Request, ctx: AuthDep) -> dict[str, Any]:
    tenant_id, user_id = viewer_of(ctx)
    muted = sorted(set(body.muted_kinds))
    if any(not KIND_RE.match(k) for k in muted):
        raise Unprocessable("Invalid notification kind in muted_kinds")
    async with request.app.state.db.tenant_session(tenant_id, ctx.actor) as s:
        current = await load_preferences(s, user_id)
        if body.expected_version is not None and body.expected_version != current["version"]:
            raise Conflict("Preferences were changed elsewhere; reload and retry")
        if current["version"] == 0:
            inserted = (
                await s.execute(
                    insert(notification_preferences)
                    .values(tenant_id=tenant_id, user_id=user_id, email_enabled=body.email_enabled, muted_kinds=muted)
                    .on_conflict_do_nothing(constraint="uq_notification_preferences_user")
                    .returning(notification_preferences.c.version)
                )
            ).scalar_one_or_none()
            if inserted is None:
                raise Conflict("Preferences were changed elsewhere; reload and retry")
            version = inserted
        else:
            version = (
                await s.execute(
                    update(notification_preferences)
                    .where(
                        notification_preferences.c.user_id == user_id,
                        notification_preferences.c.version == current["version"],
                    )
                    .values(
                        email_enabled=body.email_enabled,
                        muted_kinds=muted,
                        version=notification_preferences.c.version + 1,
                        updated_at=func.now(),
                    )
                    .returning(notification_preferences.c.version)
                )
            ).scalar_one_or_none()
            if version is None:
                raise Conflict("Preferences were changed elsewhere; reload and retry")
    return {"email_enabled": body.email_enabled, "muted_kinds": muted, "version": version}
