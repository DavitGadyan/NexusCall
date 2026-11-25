"""Shared helpers for the public routers."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_inbox_service.tables import attachments, messages
from nexus_core.config import Environment
from nexus_core.db import Database
from nexus_core.errors import Unprocessable


def db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def require_idempotency_key(value: str | None) -> str:
    if not value or len(value) > 255:
        raise Unprocessable("An Idempotency-Key header (1..255 characters) is required")
    return value


def downloadable(settings: Any, row: Any) -> bool:
    """Only scanned-clean files (or `skipped` in development/test) are ever downloadable."""
    if row["quarantined"] or not row["object_key"]:
        return False
    if row["scan_status"] == "clean":
        return True
    return row["scan_status"] == "skipped" and settings.env in (Environment.DEVELOPMENT, Environment.TEST)


async def load_messages(
    session: AsyncSession, settings: Any, *, thread_id: uuid.UUID | None = None, message_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    stmt = select(messages)
    if thread_id is not None:
        stmt = stmt.where(messages.c.thread_id == thread_id)
    if message_id is not None:
        stmt = stmt.where(messages.c.id == message_id)
    rows = (await session.execute(stmt.order_by(messages.c.received_at, messages.c.id).limit(200))).mappings().all()
    ids = [r["id"] for r in rows]
    by_message: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if ids:
        for a in (await session.execute(select(attachments).where(attachments.c.message_id.in_(ids)))).mappings().all():
            by_message.setdefault(a["message_id"], []).append({**a, "downloadable": downloadable(settings, a)})
    return [
        {**r, "content_purged": r["content_purged_at"] is not None, "attachments": by_message.get(r["id"], [])}
        for r in rows
    ]
