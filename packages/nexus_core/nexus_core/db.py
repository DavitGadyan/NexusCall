"""Async PostgreSQL access with transaction-local tenant context (build.md §5.3, ADR-0007).

All tenant-owned data access MUST go through `Database.tenant_session`, which
opens a transaction and sets `app.tenant_id` with `set_config(..., is_local=true)`
(equivalent to SET LOCAL). The setting dies with the transaction, so it can never
leak to the next user of a pooled connection. Connections are additionally rolled
back on return to the pool.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from nexus_core.context import SYSTEM_ACTOR, Actor

_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true),"
    " set_config('app.actor_id', :actor_id, true),"
    " set_config('app.actor_type', :actor_type, true)"
)


class Database:
    def __init__(self, url: str, *, pool_size: int = 10, max_overflow: int = 5, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
            echo=echo,
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def tenant_session(
        self, tenant_id: uuid.UUID | str, actor: Actor = SYSTEM_ACTOR
    ) -> AsyncIterator[AsyncSession]:
        """Transaction bound to exactly one tenant. Commits on success, rolls back on error."""
        tid = str(uuid.UUID(str(tenant_id)))  # validates format; never interpolated into SQL
        async with self._sessions() as session, session.begin():
            await session.execute(_SET_CONTEXT, {"tenant_id": tid, "actor_id": actor.id, "actor_type": actor.type})
            yield session

    @asynccontextmanager
    async def global_session(self, actor: Actor = SYSTEM_ACTOR) -> AsyncIterator[AsyncSession]:
        """Transaction with NO tenant context: only platform-scoped tables are visible.

        RLS on tenant tables evaluates `current_setting('app.tenant_id', true)` to
        NULL, so every tenant-owned row is invisible and writes are rejected.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(_SET_CONTEXT, {"tenant_id": "", "actor_id": actor.id, "actor_type": actor.type})
            yield session

    async def ping(self) -> bool:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    async def dispose(self) -> None:
        await self.engine.dispose()
