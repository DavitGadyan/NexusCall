"""Keyset pagination. Every list endpoint returns `{"items": [...], "next_cursor": str | None}`."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Query
from sqlalchemy import ColumnElement, Select, and_, or_

from nexus_core.errors import Unprocessable

Limit = Annotated[int, Query(ge=1, le=200, description="Page size")]
Cursor = Annotated[str | None, Query(max_length=512, description="Opaque cursor from the previous page")]


def encode_cursor(created_at: datetime, row_id: uuid.UUID | str | int) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "i": str(row_id)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        return datetime.fromisoformat(data["t"]), str(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise Unprocessable("Invalid cursor") from exc


def apply_keyset(
    stmt: Select[Any], created_col: ColumnElement[Any], id_col: ColumnElement[Any], cursor: str | None, limit: int
) -> Select[Any]:
    """Newest first, stable on (created_at, id). Fetches `limit + 1` rows so `build_page` can detect more."""
    if cursor:
        created, raw_id = decode_cursor(cursor)
        try:
            python_type = id_col.type.python_type
        except NotImplementedError:
            python_type = str
        try:
            row_id: Any = (
                uuid.UUID(raw_id) if python_type is uuid.UUID else int(raw_id) if python_type is int else raw_id
            )
        except ValueError as exc:
            raise Unprocessable("Invalid cursor") from exc
        stmt = stmt.where(or_(created_col < created, and_(created_col == created, id_col < row_id)))
    return stmt.order_by(created_col.desc(), id_col.desc()).limit(limit + 1)


def build_page(
    rows: list[Any], limit: int, *, created_attr: str = "created_at", id_attr: str = "id"
) -> tuple[list[Any], str | None]:
    if len(rows) <= limit:
        return rows, None
    page = rows[:limit]
    last = page[-1]
    as_mapping = isinstance(last, dict) or hasattr(last, "keys")

    def get(name: str) -> Any:
        return last[name] if as_mapping else getattr(last, name)

    return page, encode_cursor(get(created_attr), get(id_attr))
