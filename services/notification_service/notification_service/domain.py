"""In-app notifications: audience rules, idempotent creation, live hints and the SSE relay filter.

A notification is addressed either to one user or to "everyone in the tenant holding permission X". The
viewer's permissions come from tenant-service at request time, so a revoked permission immediately hides
permission-addressed notifications (nothing is fanned out into per-user rows).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import ColumnElement, false, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.authz import has_permission
from nexus_core.realtime import ui_channel
from notification_service.tables import notifications

log = logging.getLogger(__name__)

PERMISSION_RE = re.compile(r"^[a-z][a-z0-9_]*(:[a-z0-9_]+){1,3}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_.]{1,31}$")
LINK_RE = re.compile(r"^/[A-Za-z0-9/_?=&.%-]*$")
UI_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def notify_channel(tenant_id: uuid.UUID | str) -> str:
    return f"notify:{uuid.UUID(str(tenant_id))}"


def user_uuid(sub: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(sub)
    except ValueError:
        return None


def clean_text(value: str, limit: int) -> str:
    """Plain text only (the UI renders it as text); control characters removed, length bounded."""
    return _CONTROL.sub("", value).strip()[:limit]


def visible_to(user_id: uuid.UUID | None, permissions: Iterable[str]) -> ColumnElement[bool]:
    """SQL predicate: addressed to the user, or to a permission the viewer holds (wildcards supported)."""
    c = notifications.c
    conds: list[ColumnElement[bool]] = []
    if user_id is not None:
        conds.append(c.user_id == user_id)
    perms = set(permissions)
    concrete = sorted(p for p in perms if not p.endswith("*"))
    if concrete:
        conds.append(c.audience_permission.in_(concrete))
    for p in sorted(perms - set(concrete)):
        prefix = p[:-1]
        if prefix:
            conds.append(c.audience_permission.startswith(prefix, autoescape=True))
        else:
            conds.append(c.audience_permission.isnot(None))
    return or_(*conds) if conds else false()


async def create_notification(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    title: str,
    body: str,
    source: str,
    user_id: uuid.UUID | None = None,
    permission: str | None = None,
    link: str | None = None,
    dedupe_key: str | None = None,
) -> uuid.UUID | None:
    """Insert one notification. Returns None when `dedupe_key` was already used in this tenant."""
    if (user_id is None) == (permission is None):
        raise ValueError("exactly one of user_id / permission is required")
    if permission is not None and not PERMISSION_RE.match(permission):
        raise ValueError("invalid audience permission")
    if not KIND_RE.match(kind):
        raise ValueError("invalid kind")
    if link is not None and (len(link) > 300 or not LINK_RE.match(link)):
        raise ValueError("invalid link")
    return (
        await session.execute(
            insert(notifications)
            .values(
                tenant_id=tenant_id,
                user_id=user_id,
                audience_permission=permission,
                kind=kind,
                title=clean_text(title, 200),
                body=clean_text(body, 1000),
                link=link,
                dedupe_key=dedupe_key,
                source=source[:64],
            )
            .on_conflict_do_nothing(constraint="uq_notifications_dedupe")
            .returning(notifications.c.id)
        )
    ).scalar_one_or_none()


async def publish_hint(
    redis: Redis | None,
    tenant_id: uuid.UUID,
    notification_id: uuid.UUID,
    *,
    kind: str,
    user_id: uuid.UUID | None,
    permission: str | None,
) -> None:
    """Best-effort live hint for the SSE relay; ids and audience only (never title or body)."""
    if redis is None:
        return
    try:
        await redis.publish(
            notify_channel(tenant_id),
            json.dumps(
                {
                    "id": str(notification_id),
                    "kind": kind,
                    "user_id": str(user_id) if user_id else None,
                    "permission": permission,
                }
            ),
        )
    except Exception:  # noqa: BLE001 - live hints are optional; the list endpoint is authoritative
        log.warning("notification_hint_failed", extra={"notification_id": str(notification_id)})


@dataclass(slots=True)
class Viewer:
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    permissions: frozenset[str]
    muted_kinds: frozenset[str] = field(default_factory=frozenset)

    def can(self, permission: str) -> bool:
        return has_permission(self.permissions, permission)


def _decode(raw: Any) -> Any:
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode()
    if not isinstance(raw, str) or len(raw) > 4096:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def relay(channel: str, raw: Any, viewer: Viewer) -> dict[str, Any] | None:
    """Decide what (if anything) one Redis message becomes on this viewer's stream. Fails closed:
    malformed messages and UI events without a permission tag are dropped."""
    msg = _decode(raw)
    if not isinstance(msg, dict):
        return None
    if channel == ui_channel(viewer.tenant_id):
        event, permission, data = msg.get("type"), msg.get("permission"), msg.get("data", {})
        if not (isinstance(event, str) and UI_TYPE_RE.match(event) and isinstance(permission, str) and permission):
            return None
        if not viewer.can(permission):
            return None
        if not isinstance(data, dict) or len(data) > 8:
            return None
        if any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 64 for k, v in data.items()):
            return None
        return {"type": "ui", "event": event, "data": data}
    if channel == notify_channel(viewer.tenant_id):
        nid, kind = msg.get("id"), msg.get("kind")
        try:
            nid = str(uuid.UUID(str(nid)))
        except ValueError:
            return None
        if kind in viewer.muted_kinds:
            return None
        target_user, permission = msg.get("user_id"), msg.get("permission")
        addressed = (target_user is not None and viewer.user_id is not None and target_user == str(viewer.user_id)) or (
            isinstance(permission, str) and bool(permission) and viewer.can(permission)
        )
        return {"type": "notification", "id": nid} if addressed else None
    return None
