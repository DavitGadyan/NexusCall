"""Keyset pagination with a selectable sort key (contacts, leads). Cursors are opaque and bound to the sort."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, or_

from nexus_core.errors import Unprocessable


def encode(sort: str, direction: str, value: Any, row_id: uuid.UUID) -> str:
    raw = json.dumps(
        {"s": sort, "d": direction, "v": value.isoformat() if isinstance(value, datetime) else value, "i": str(row_id)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(data, dict) or not {"s", "d", "v", "i"} <= set(data):
            raise ValueError
        data["i"] = uuid.UUID(str(data["i"]))
        return data
    except (ValueError, TypeError) as exc:
        raise Unprocessable("Invalid cursor") from exc


def apply(
    stmt: Select[Any],
    *,
    sort: str,
    direction: str,
    key: ColumnElement[Any],
    kind: str,
    id_col: ColumnElement[Any],
    cursor: str | None,
    limit: int,
) -> Select[Any]:
    """`key` must be non-null (coalesce nullable columns). `kind`: datetime | int | str."""
    if cursor:
        data = _decode(cursor)
        if data["s"] != sort or data["d"] != direction:
            raise Unprocessable("Cursor does not match the requested sort")
        try:
            value: Any = (
                datetime.fromisoformat(data["v"])
                if kind == "datetime"
                else int(data["v"])
                if kind == "int"
                else str(data["v"])
            )
        except (TypeError, ValueError) as exc:
            raise Unprocessable("Invalid cursor") from exc
        if direction == "desc":
            stmt = stmt.where(or_(key < value, and_(key == value, id_col < data["i"])))
        else:
            stmt = stmt.where(or_(key > value, and_(key == value, id_col > data["i"])))
    order = (key.desc(), id_col.desc()) if direction == "desc" else (key.asc(), id_col.asc())
    return stmt.add_columns(key.label("_sort")).order_by(*order).limit(limit + 1)


def page(rows: list[Any], limit: int, *, sort: str, direction: str) -> tuple[list[Any], str | None]:
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    last = rows[-1]
    return rows, encode(sort, direction, last["_sort"], last["id"])
