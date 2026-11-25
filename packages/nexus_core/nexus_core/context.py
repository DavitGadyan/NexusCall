"""Per-request / per-job context propagated through logs, events and tasks."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
actor_var: ContextVar[str | None] = ContextVar("actor", default=None)


@dataclass(frozen=True, slots=True)
class Actor:
    """Who performs an action. `type` is user | service | system."""

    type: str
    id: str

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"


SYSTEM_ACTOR = Actor(type="system", id="system")


def new_id() -> str:
    return str(uuid.uuid4())


def current_correlation_id() -> str:
    cid = correlation_id_var.get()
    if cid is None:
        cid = new_id()
        correlation_id_var.set(cid)
    return cid
