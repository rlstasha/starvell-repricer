import asyncio
import random
import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis


ADAPTIVE_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0)


def adaptive_backoff_seconds(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    index = min(consecutive_errors, len(ADAPTIVE_BACKOFF_SECONDS)) - 1
    return ADAPTIVE_BACKOFF_SECONDS[index]


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


class CompositeRateLimiter:
    """Profile limiter + global limiter + small pacing/backoff guard."""

    def __init__(
        self,
        *,
        profile_limiter: RateLimiter,
        global_limiter: RateLimiter,
        burst_limiter: RateLimiter | None = None,
        min_delay_ms: int = 0,
        max_delay_ms: int = 5000,
        jitter_ms: int = 0,
        backoff_factor: float = 2.0,
        sleeper=asyncio.sleep,
    ):
        self.profile_limiter = profile_limiter
        self.global_limiter = global_limiter
        self.burst_limiter = burst_limiter
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.jitter_ms = jitter_ms
        self.backoff_factor = backoff_factor
        self.sleeper = sleeper
        self._extra_delay_ms = 0.0
        self._consecutive_errors = 0

    async def try_acquire(self, cost: int = 1) -> bool:
        if self.burst_limiter and not await self.burst_limiter.try_acquire(cost):
            return False
        if not await self.profile_limiter.try_acquire(cost):
            return False
        return await self.global_limiter.try_acquire(cost)

    async def acquire(self, cost: int = 1) -> None:
        if self.burst_limiter is not None:
            await self.burst_limiter.acquire(cost)
        await self.profile_limiter.acquire(cost)
        await self.global_limiter.acquire(cost)
        await self._sleep_after_request()

    async def current_usage(self) -> int:
        return await self.profile_limiter.current_usage()

    def apply_backoff(self, error_kind: str | None = None) -> float:
        self._consecutive_errors += 1
        adaptive_delay_ms = adaptive_backoff_seconds(self._consecutive_errors) * 1000
        self._extra_delay_ms = adaptive_delay_ms
        return self._extra_delay_ms / 1000

    def reset_backoff(self) -> None:
        self._extra_delay_ms = 0.0
        self._consecutive_errors = 0

    async def _sleep_after_request(self) -> None:
        delay_ms = self.min_delay_ms + self._extra_delay_ms
        if self.jitter_ms > 0:
            delay_ms += random.uniform(0, self.jitter_ms)
        delay_ms = min(delay_ms, self.max_delay_ms)
        if delay_ms > 0:
            await self.sleeper(delay_ms / 1000)


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
