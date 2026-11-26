"""Redis-backed rate limiting primitives: fixed window, token bucket and concurrency slots.

All functions fail *closed* for safety-critical limits when asked (`fail_open=False`) and never
raise on a missing Redis in tests (pass `redis=None` to disable).
"""

from __future__ import annotations

import time
from typing import Any

import anyio
from redis.asyncio import Redis

from nexus_core.errors import TooManyRequests

_BUCKET_LUA = """
local key, rate, burst, now, cost = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens, ts = tonumber(data[1]), tonumber(data[2])
if tokens == nil then tokens = burst; ts = now end
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens >= cost then tokens = tokens - cost else wait = (cost - tokens) / rate end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return tostring(wait)
"""

_ACQUIRE_LUA = """
local key, now, ttl, max_slots, member = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZSCORE', key, member) then
  redis.call('ZADD', key, now + ttl, member)
  return 1
end
if redis.call('ZCARD', key) >= max_slots then return 0 end
redis.call('ZADD', key, now + ttl, member)
redis.call('EXPIRE', key, ttl + 60)
return 1
"""


async def allow_fixed_window(redis: Redis | None, key: str, limit: int, window_seconds: int) -> bool:
    """True while the caller is within `limit` events per window."""
    if redis is None:
        return True
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return int(count) <= limit


async def enforce_fixed_window(
    redis: Redis | None, key: str, limit: int, window_seconds: int, message: str = "Too many requests; try again later"
) -> None:
    if not await allow_fixed_window(redis, key, limit, window_seconds):
        raise TooManyRequests(message)


async def take_token(redis: Redis | None, key: str, *, rate_per_second: float, burst: int, cost: int = 1) -> float:
    """Token bucket. Returns 0.0 when the cost was taken, otherwise the seconds to wait (nothing taken)."""
    if redis is None:
        return 0.0
    result: Any = await redis.eval(  # type: ignore[misc]
        _BUCKET_LUA, 1, key, str(rate_per_second), str(burst), str(time.time()), str(cost)
    )
    return float(result.decode() if isinstance(result, bytes) else result)


async def acquire_slot(redis: Redis | None, key: str, member: str, *, max_slots: int, ttl_seconds: int) -> bool:
    """Concurrency semaphore with per-holder expiry (a crashed holder frees its slot after `ttl_seconds`).

    Re-acquiring with the same `member` refreshes the lease and is idempotent."""
    if redis is None:
        return True
    result: Any = await redis.eval(  # type: ignore[misc]
        _ACQUIRE_LUA, 1, key, str(time.time()), str(ttl_seconds), str(max_slots), member
    )
    return bool(result)


async def release_slot(redis: Redis | None, key: str, member: str) -> None:
    """Shielded: callers release in `finally`, often while their task is being cancelled (a client that
    disconnects from a stream). Without the shield anyio re-raises the cancellation at this await, the
    ZREM never runs, and the slot stays taken until its lease expires."""
    if redis is not None:
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            await redis.zrem(key, member)


async def slots_in_use(redis: Redis | None, key: str) -> int:
    if redis is None:
        return 0
    await redis.zremrangebyscore(key, "-inf", time.time())
    return int(await redis.zcard(key))
