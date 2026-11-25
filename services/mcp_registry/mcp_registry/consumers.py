"""Event consumers. The registry reacts to exactly one event: tool-execution reporting that an MCP server
answered with an identity or version that differs from its reviewed pin (owasp LLM04). The only effect is
fail-safe: the server is disabled. No event can ever enable a server or add a grant."""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.deps import audit
from mcp_registry.tables import mcp_server_versions, mcp_servers
from nexus_core.context import Actor
from nexus_core.messaging import Handler
from nexus_events import EventEnvelope

log = logging.getLogger(__name__)
IDENTITY_MISMATCH = "mcp_server_identity_mismatch"
_SERVER = re.compile(r"server_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


async def on_security_event(event: EventEnvelope, session: AsyncSession) -> None:
    if event.payload.get("kind") != IDENTITY_MISMATCH:
        return
    match = _SERVER.search(str(event.payload.get("detail", "")))
    if match is None:
        log.warning("mcp_mismatch_event_without_server", extra={"event_id": str(event.event_id)})
        return
    server_id = uuid.UUID(match.group(1))
    disabled = (
        await session.execute(
            update(mcp_servers)
            .where(mcp_servers.c.id == server_id, mcp_servers.c.enabled.is_(True))
            .values(
                enabled=False,
                health="identity_mismatch",
                disabled_reason="reported identity or version differs from the reviewed pin",
                row_version=mcp_servers.c.row_version + 1,
                updated_at=func.now(),
            )
            .returning(mcp_servers.c.id, mcp_servers.c.key)
        )
    ).first()
    if disabled is None:  # already disabled or unknown: idempotent
        return
    revision = (
        await session.execute(
            select(func.coalesce(func.max(mcp_server_versions.c.revision), 0) + 1).where(
                mcp_server_versions.c.server_id == server_id
            )
        )
    ).scalar_one()
    await session.execute(
        mcp_server_versions.insert().values(
            server_id=server_id,
            revision=revision,
            change="auto_disabled",
            snapshot={"reason": IDENTITY_MISMATCH, "event_id": str(event.event_id)},
            changed_by="system:mcp-pin-guard",
        )
    )
    await audit(
        session,
        Actor("system", "mcp-pin-guard"),
        "ai.mcp_server.auto_disabled",
        "mcp_server",
        server_id,
        tenant_id=None,  # platform resource; the triggering tenant stays in the event id
        outcome="success",
        key=disabled.key,
        cause=IDENTITY_MISMATCH,
        event_id=str(event.event_id),
    )


HANDLERS: dict[str, Handler] = {"ai.security_event": on_security_event}
ALLOWED_PRODUCERS: dict[str, set[str]] = {"ai.security_event": {"tool-execution"}}
