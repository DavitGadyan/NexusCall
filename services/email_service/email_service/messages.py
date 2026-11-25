"""Message rows and state transitions. Every transition is a guarded `UPDATE ... WHERE state IN (sources)`
inside the caller's tenant session, so concurrent workers/webhooks can never move a message backwards."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from email_service.deps import emit
from email_service.state_machine import sources_for
from email_service.tables import email_messages, message_directory, tenant_activity
from nexus_core.context import Actor

# States other contexts care about (analytics, crm). Internal hops (pending/queued/sending) are not published.
PUBLISHED_STATES = frozenset({"accepted", "delivered", "bounced", "complained", "rejected", "unsubscribed", "failed"})
TIMESTAMP_FOR = {"queued": "queued_at", "accepted": "accepted_at", "delivered": "delivered_at"}


def task_for(category: str) -> str:
    return "emails.campaign_send" if category == "campaign" else "emails.send_message"


async def touch_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await session.execute(
        insert(tenant_activity)
        .values(tenant_id=tenant_id)
        .on_conflict_do_update(index_elements=["tenant_id"], set_={"last_activity_at": func.now()})
    )


async def register_message(session: AsyncSession, tenant_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """Ids-only directory row so provider events can be routed to the owning tenant."""
    await session.execute(
        insert(message_directory).values(message_id=message_id, tenant_id=tenant_id).on_conflict_do_nothing()
    )


async def insert_message(session: AsyncSession, tenant_id: uuid.UUID, **values: Any) -> uuid.UUID:
    message_id = values.pop("id", None) or uuid.uuid4()
    await session.execute(insert(email_messages).values(id=message_id, tenant_id=tenant_id, **values))
    await register_message(session, tenant_id, message_id)
    await touch_tenant(session, tenant_id)
    return message_id  # type: ignore[no-any-return]


async def transition(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    message_id: uuid.UUID,
    target: str,
    actor: Actor,
    values: dict[str, Any] | None = None,
    only_from: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """Move a message to `target` if the state machine allows it from its current state. Returns the row
    (after the update) or None when the message was not in an allowed source state."""
    sources = only_from or sources_for(target)
    data: dict[str, Any] = {"state": target, "state_changed_at": func.now(), "updated_at": func.now(), **(values or {})}
    if target in TIMESTAMP_FOR:
        data.setdefault(TIMESTAMP_FOR[target], func.now())
    row = (
        (
            await session.execute(
                update(email_messages)
                .where(email_messages.c.id == message_id, email_messages.c.state.in_(sources))
                .values(**data)
                .returning(
                    email_messages.c.id,
                    email_messages.c.category,
                    email_messages.c.campaign_id,
                    email_messages.c.contact_id,
                    email_messages.c.state,
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if target in PUBLISHED_STATES:
        await emit(
            session,
            "email.message.state_changed",
            tenant_id=tenant_id,
            actor=actor,
            payload={
                "message_id": str(row["id"]),
                "state": target,
                "category": row["category"],
                "campaign_id": str(row["campaign_id"]) if row["campaign_id"] else None,
            },
        )
    return dict(row)


async def load_message(session: AsyncSession, message_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
    stmt = select(email_messages).where(email_messages.c.id == message_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    return dict(row) if row else None
