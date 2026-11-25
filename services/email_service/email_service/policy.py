"""Send-time policy: local suppression memory, the atomic tenant daily quota and token buckets."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from email_service.tables import email_messages, unsubscribe_tokens, usage_counters
from nexus_core.ratelimit import take_token

MARKETING_ONLY_REASONS = frozenset({"opt_out"})


def blocks(category: str, reasons: list[str] | set[str]) -> bool:
    """Marketing and transactional streams are separate: an unsubscribe (opt-out) stops campaign mail only.
    Hard bounces, complaints and manual/legal suppressions stop every category."""
    if not reasons:
        return False
    return category == "campaign" or not set(reasons) <= MARKETING_ONLY_REASONS


async def local_block_reason(session: AsyncSession, to_hash: str, category: str) -> str | None:
    """This service's own memory of opt-outs, hard bounces and complaints for the address. It applies at once,
    before the crm suppression record (synced asynchronously) exists."""
    rows = (
        await session.execute(
            select(email_messages.c.state, email_messages.c.suppression_reason)
            .where(
                email_messages.c.to_hash == to_hash,
                or_(
                    email_messages.c.state.in_(("bounced", "complained", "unsubscribed")),
                    email_messages.c.suppression_reason.is_not(None),
                ),
            )
            .limit(20)
        )
    ).all()
    reasons = {str(r.suppression_reason or {"bounced": "bounce", "complained": "complaint"}.get(r.state, "opt_out"))
               for r in rows}  # fmt: skip
    if blocks(category, reasons):
        return sorted(reasons - MARKETING_ONLY_REASONS or reasons)[0]
    if category != "campaign":
        return None
    unsubscribed = (
        await session.execute(
            select(
                exists().where(unsubscribe_tokens.c.address_hash == to_hash, unsubscribe_tokens.c.used_at.is_not(None))
            )
        )
    ).scalar_one()
    return "opt_out" if unsubscribed else None


async def take_daily_quota(
    session: AsyncSession, tenant_id: uuid.UUID, quota: int, *, today: date | None = None
) -> bool:
    """Atomically count one send against the tenant's daily quota. False when the quota is used up."""
    day = today or datetime.now(UTC).date()
    stmt = (
        insert(usage_counters)
        .values(tenant_id=tenant_id, day=day, sent=1)
        .on_conflict_do_update(
            index_elements=["tenant_id", "day"],
            set_={"sent": usage_counters.c.sent + 1, "updated_at": datetime.now(UTC)},
            where=usage_counters.c.sent < quota,
        )
        .returning(usage_counters.c.sent)
    )
    sent = (await session.execute(stmt)).scalar_one_or_none()
    return sent is not None and sent <= quota


def seconds_until_next_day(now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return (tomorrow - now).total_seconds() + 60


class RateLimiter(Protocol):
    async def take(self, key: str, *, rate_per_second: float, burst: int) -> float:
        """0.0 when a token was taken, otherwise the seconds to wait."""


class RedisRateLimiter:
    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis

    async def take(self, key: str, *, rate_per_second: float, burst: int) -> float:
        return await take_token(self._redis, key, rate_per_second=rate_per_second, burst=burst)


def limiter_for(deps: Any) -> RateLimiter:
    custom = getattr(deps, "limiter", None)
    return custom if custom is not None else RedisRateLimiter(deps.redis)
