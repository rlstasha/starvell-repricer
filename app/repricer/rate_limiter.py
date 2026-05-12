import asyncio
import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def acquire(self, cost: int = 1) -> None:
        ...

    async def try_acquire(self, cost: int = 1) -> bool:
        ...

    async def current_usage(self) -> int:
        ...


class RedisFixedWindowRateLimiter:
    """Simple shared fixed-window limiter for external Starvell requests."""

    def __init__(
        self,
        redis: Redis,
        *,
        limit: int = 100,
        window_seconds: int = 60,
        key_prefix: str = "repricer:rate-limit",
    ):
        self.redis = redis
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix

    def _window_key(self) -> str:
        window = int(time.time() // self.window_seconds)
        return f"{self.key_prefix}:{window}"

    def _seconds_until_next_window(self) -> float:
        return self.window_seconds - (time.time() % self.window_seconds)

    async def try_acquire(self, cost: int = 1) -> bool:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        key = self._window_key()
        pipe = self.redis.pipeline(transaction=True)
        pipe.incrby(key, cost)
        pipe.expire(key, self.window_seconds + 5)
        count, _ = await pipe.execute()
        if int(count) <= self.limit:
            return True
        await self.redis.decrby(key, cost)
        return False

    async def acquire(self, cost: int = 1) -> None:
        while not await self.try_acquire(cost):
            await asyncio.sleep(max(self._seconds_until_next_window(), 0.05))

    async def current_usage(self) -> int:
        value = await self.redis.get(self._window_key())
        return int(value or 0)


class InMemoryFixedWindowRateLimiter:
    """Test-friendly limiter with the same fixed-window semantics."""

    def __init__(
        self,
        *,
        limit: int = 100,
        window_seconds: int = 60,
        clock=time.monotonic,
        sleeper=asyncio.sleep,
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.sleeper = sleeper
        self._counts: dict[int, int] = defaultdict(int)

    def _window(self) -> int:
        return int(self.clock() // self.window_seconds)

    def _seconds_until_next_window(self) -> float:
        return self.window_seconds - (self.clock() % self.window_seconds)

    async def try_acquire(self, cost: int = 1) -> bool:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        window = self._window()
        if self._counts[window] + cost > self.limit:
            return False
        self._counts[window] += cost
        return True

    async def acquire(self, cost: int = 1) -> None:
        while not await self.try_acquire(cost):
            await self.sleeper(max(self._seconds_until_next_window(), 0.05))

    async def current_usage(self) -> int:
        return self._counts[self._window()]
