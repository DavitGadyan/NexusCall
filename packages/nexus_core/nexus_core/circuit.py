"""Circuit breaker for external providers (build.md §10.3). Redis-backed so every replica shares state
and the admin UI can display it; falls back to in-process state when Redis is absent (tests)."""

from __future__ import annotations

import time
from typing import Literal

from redis.asyncio import Redis

State = Literal["closed", "open", "half_open"]


class CircuitOpen(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"circuit '{name}' is open")
        self.name = name


class CircuitBreaker:
    def __init__(
        self,
        redis: Redis | None,
        name: str,
        *,
        failure_threshold: int = 5,
        window_seconds: int = 60,
        open_seconds: int = 30,
    ) -> None:
        self.redis, self.name = redis, name
        self.failure_threshold, self.window, self.open_seconds = failure_threshold, window_seconds, open_seconds
        self._local_failures: list[float] = []
        self._local_open_until = 0.0

    @property
    def _fail_key(self) -> str:
        return f"cb:{self.name}:failures"

    @property
    def _open_key(self) -> str:
        return f"cb:{self.name}:open_until"

    async def _open_until(self) -> float:
        if self.redis is None:
            return self._local_open_until
        raw = await self.redis.get(self._open_key)
        return float(raw) if raw else 0.0

    async def state(self) -> State:
        open_until = await self._open_until()
        if open_until == 0.0:
            return "closed"
        return "open" if time.time() < open_until else "half_open"

    async def check(self) -> None:
        """Raise `CircuitOpen` while open. In half-open state calls are let through as probes."""
        if await self.state() == "open":
            raise CircuitOpen(self.name)

    async def record_success(self) -> None:
        if self.redis is None:
            self._local_failures.clear()
            self._local_open_until = 0.0
            return
        await self.redis.delete(self._fail_key, self._open_key)

    async def record_failure(self) -> bool:
        """Returns True when this failure opened (or re-opened) the circuit."""
        now = time.time()
        half_open = await self.state() == "half_open"
        if self.redis is None:
            self._local_failures = [t for t in self._local_failures if t > now - self.window] + [now]
            if half_open or len(self._local_failures) >= self.failure_threshold:
                self._local_open_until = now + self.open_seconds
                return True
            return False
        count = await self.redis.incr(self._fail_key)
        if count == 1:
            await self.redis.expire(self._fail_key, self.window)
        if half_open or int(count) >= self.failure_threshold:
            await self.redis.set(self._open_key, now + self.open_seconds, ex=self.open_seconds * 20)
            return True
        return False
