import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Mapping, Protocol

from redis.asyncio import Redis


ADAPTIVE_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0)
TRANSPORT_BACKOFF_SECONDS = (0.2, 0.5, 1.0, 2.0, 3.0)
TOKEN_BUCKET_SCRIPT = """
local bucket_key = KEYS[1]
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call("HMGET", bucket_key, "tokens", "updated_at")
local tokens = tonumber(data[1])
local updated_at = tonumber(data[2])

if tokens == nil then
  tokens = capacity
end
if updated_at == nil then
  updated_at = now
end

local elapsed = math.max(now - updated_at, 0)
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= cost then
  tokens = tokens - cost
  redis.call("HMSET", bucket_key, "tokens", tokens, "updated_at", now)
  redis.call("EXPIRE", bucket_key, ttl)
  return {1, tokens, 0}
end

local wait_seconds = (cost - tokens) / rate
redis.call("HMSET", bucket_key, "tokens", tokens, "updated_at", now)
redis.call("EXPIRE", bucket_key, ttl)
return {0, tokens, wait_seconds}
"""
SLIDING_WINDOW_SCRIPT = """
local window_key = KEYS[1]
local seq_key = KEYS[2]
local now = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local cutoff = now - window_seconds
redis.call("ZREMRANGEBYSCORE", window_key, "-inf", cutoff)

local current = redis.call("ZCARD", window_key)
if current + cost <= limit then
  for i = 1, cost do
    local seq = redis.call("INCR", seq_key)
    redis.call("ZADD", window_key, now, tostring(now) .. ":" .. tostring(seq))
  end
  redis.call("EXPIRE", window_key, ttl)
  redis.call("EXPIRE", seq_key, ttl)
  return {1, current + cost, 0}
end

local oldest = redis.call("ZRANGE", window_key, 0, 0, "WITHSCORES")
local wait_seconds = 0.05
if oldest[2] ~= nil then
  wait_seconds = math.max((tonumber(oldest[2]) + window_seconds) - now, 0.05)
end

redis.call("EXPIRE", window_key, ttl)
redis.call("EXPIRE", seq_key, ttl)
return {0, current, wait_seconds}
"""
MULTI_SLIDING_WINDOW_SCRIPT = """
local seq_key = KEYS[1]
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local windows_count = tonumber(ARGV[4])

local max_wait_seconds = 0
local denied_index = 0
local denied_current = 0

for i = 1, windows_count do
  local window_key = KEYS[i + 1]
  local base = 5 + ((i - 1) * 2)
  local limit = tonumber(ARGV[base])
  local window_seconds = tonumber(ARGV[base + 1])
  local cutoff = now - window_seconds

  redis.call("ZREMRANGEBYSCORE", window_key, "-inf", cutoff)
  local current = redis.call("ZCARD", window_key)
  if current + cost > limit then
    local oldest = redis.call("ZRANGE", window_key, 0, 0, "WITHSCORES")
    local wait_seconds = 0.05
    if oldest[2] ~= nil then
      wait_seconds = math.max((tonumber(oldest[2]) + window_seconds) - now, 0.05)
    end
    if wait_seconds > max_wait_seconds then
      max_wait_seconds = wait_seconds
      denied_index = i
      denied_current = current
    end
  end
end

if denied_index > 0 then
  for i = 1, windows_count do
    redis.call("EXPIRE", KEYS[i + 1], ttl)
  end
  redis.call("EXPIRE", seq_key, ttl)
  return {0, denied_current, max_wait_seconds, denied_index}
end

for i = 1, windows_count do
  local window_key = KEYS[i + 1]
  for item = 1, cost do
    local seq = redis.call("INCR", seq_key)
    redis.call("ZADD", window_key, now, tostring(now) .. ":" .. tostring(i) .. ":" .. tostring(seq))
  end
  redis.call("EXPIRE", window_key, ttl)
end
redis.call("EXPIRE", seq_key, ttl)

return {1, 0, 0, 0}
"""


def adaptive_backoff_seconds(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    index = min(consecutive_errors, len(ADAPTIVE_BACKOFF_SECONDS)) - 1
    return ADAPTIVE_BACKOFF_SECONDS[index]


def transport_backoff_seconds(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    index = min(consecutive_errors, len(TRANSPORT_BACKOFF_SECONDS)) - 1
    return TRANSPORT_BACKOFF_SECONDS[index]


class RateLimiter(Protocol):
    async def acquire(self, cost: int = 1) -> None:
        ...

    async def try_acquire(self, cost: int = 1) -> bool:
        ...

    async def current_usage(self) -> int:
        ...


@dataclass(frozen=True)
class RateLimitSnapshot:
    configured_limit_per_minute: int
    effective_limit_per_minute: int
    current_usage: int
    backoff_active: bool
    last_429_at: datetime | None
    retry_after_until: datetime | None
    target_limit_per_minute: int | None = None


@dataclass(frozen=True)
class RateLimitBackoffEvent:
    old_effective_limit_per_minute: int
    new_effective_limit_per_minute: int
    reason: str
    recovery_eta_seconds: float
    retry_after_seconds: float | None
    consecutive_429s: int


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


class RedisTokenBucketRateLimiter:
    """Redis-backed token bucket limiter with a per-minute usage counter."""

    def __init__(
        self,
        redis: Redis,
        *,
        limit: int = 100,
        key_prefix: str = "repricer:token-bucket",
        sleeper=asyncio.sleep,
    ):
        self.redis = redis
        self.limit = limit
        self.key_prefix = key_prefix
        self.sleeper = sleeper
        self._last_wait_seconds = 0.05

    def _bucket_key(self) -> str:
        return f"{self.key_prefix}:bucket"

    def _usage_key(self) -> str:
        window = int(time.time() // 60)
        return f"{self.key_prefix}:usage:{window}"

    async def try_acquire(self, cost: int = 1) -> bool:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        limit = max(int(self.limit), 1)
        rate_per_second = limit / 60
        result = await self.redis.eval(
            TOKEN_BUCKET_SCRIPT,
            1,
            self._bucket_key(),
            time.time(),
            rate_per_second,
            limit,
            cost,
            120,
        )
        allowed = int(result[0]) == 1
        self._last_wait_seconds = max(float(result[2] or 0.05), 0.05)
        return allowed

    async def acquire(self, cost: int = 1) -> None:
        while not await self.try_acquire(cost):
            await self.sleeper(self._last_wait_seconds)

    async def current_usage(self) -> int:
        value = await self.redis.get(self._usage_key())
        return int(value or 0)

    async def _increment_usage(self, cost: int) -> None:
        key = self._usage_key()
        pipe = self.redis.pipeline(transaction=True)
        pipe.incrby(key, cost)
        pipe.expire(key, 125)
        await pipe.execute()


class RedisSlidingWindowRateLimiter:
    """Redis-backed exact sliding-window limiter for shared request ceilings."""

    def __init__(
        self,
        redis: Redis,
        *,
        limit: int = 100,
        window_seconds: float = 60.0,
        key_prefix: str = "repricer:sliding-window",
        sleeper=asyncio.sleep,
    ):
        self.redis = redis
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix
        self.sleeper = sleeper
        self._last_wait_seconds = 0.05

    def _window_key(self) -> str:
        return f"{self.key_prefix}:events"

    def _seq_key(self) -> str:
        return f"{self.key_prefix}:seq"

    async def try_acquire(self, cost: int = 1) -> bool:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        allowed, _, wait_seconds = await self._run_script(max(int(self.limit), 1), cost)
        self._last_wait_seconds = max(float(wait_seconds or 0.05), 0.05)
        return bool(allowed)

    async def acquire(self, cost: int = 1) -> None:
        while not await self.try_acquire(cost):
            await self.sleeper(self._last_wait_seconds)

    async def current_usage(self) -> int:
        now = time.time()
        window_key = self._window_key()
        await self.redis.zremrangebyscore(window_key, "-inf", now - self.window_seconds)
        return int(await self.redis.zcard(window_key))

    async def _run_script(self, limit: int, cost: int) -> tuple[int, int, float]:
        result = await self.redis.eval(
            SLIDING_WINDOW_SCRIPT,
            2,
            self._window_key(),
            self._seq_key(),
            time.time(),
            self.window_seconds,
            limit,
            cost,
            int(self.window_seconds * 2 + 10),
        )
        return int(result[0]), int(result[1]), float(result[2] or 0.0)


class RedisAdaptiveTokenBucketRateLimiter:
    """Shared account/session limiter that learns a safe effective request limit."""

    def __init__(
        self,
        redis: Redis,
        *,
        configured_limit_per_minute: int,
        initial_effective_limit_per_minute: int,
        min_limit_per_minute: int = 60,
        target_limit_per_minute: int | None = None,
        target_min_limit_per_minute: int = 260,
        target_decrease_step_per_minute: int = 10,
        target_ramp_idle_seconds: float = 300.0,
        decrease_step_per_minute: int = 30,
        ramp_step_per_minute: int = 10,
        ramp_idle_seconds: float = 600.0,
        isolated_429_window_seconds: float = 120.0,
        key_prefix: str = "repricer:account-token-limit",
        sleeper=asyncio.sleep,
    ):
        self.redis = redis
        self.configured_limit_per_minute = configured_limit_per_minute
        self.initial_effective_limit_per_minute = min(
            initial_effective_limit_per_minute,
            configured_limit_per_minute,
        )
        self.min_limit_per_minute = min_limit_per_minute
        self.initial_target_limit_per_minute = min(
            target_limit_per_minute or configured_limit_per_minute,
            configured_limit_per_minute,
        )
        self.target_min_limit_per_minute = min(
            max(target_min_limit_per_minute, min_limit_per_minute),
            self.initial_target_limit_per_minute,
        )
        self.target_decrease_step_per_minute = target_decrease_step_per_minute
        self.target_ramp_idle_seconds = target_ramp_idle_seconds
        self.decrease_step_per_minute = decrease_step_per_minute
        self.ramp_step_per_minute = ramp_step_per_minute
        self.ramp_idle_seconds = ramp_idle_seconds
        self.isolated_429_window_seconds = isolated_429_window_seconds
        self.key_prefix = key_prefix
        self.sleeper = sleeper
        self._last_wait_seconds = 0.05

    def _state_key(self) -> str:
        return f"{self.key_prefix}:state"

    def _bucket_key(self) -> str:
        return f"{self.key_prefix}:bucket"

    def _window_key(self) -> str:
        return f"{self.key_prefix}:events"

    def _seq_key(self) -> str:
        return f"{self.key_prefix}:seq"

    def _usage_key(self) -> str:
        window = int(time.time() // 60)
        return f"{self.key_prefix}:usage:{window}"

    async def try_acquire(self, cost: int = 1) -> bool:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        state = await self._state()
        now = time.time()
        retry_after_until = _float_state(state, "retry_after_until_epoch")
        if retry_after_until and retry_after_until > now:
            self._last_wait_seconds = max(retry_after_until - now, 0.05)
            return False

        state = await self._maybe_ramp_up(state=state, now=now)
        effective_limit = _int_state(
            state,
            "effective_limit_per_minute",
            self.initial_effective_limit_per_minute,
        )
        target_limit = _int_state(
            state,
            "target_limit_per_minute",
            self.initial_target_limit_per_minute,
        )
        return await self._try_sliding_window_acquire(min(effective_limit, target_limit), cost)

    async def active_limit_state(self) -> tuple[int, float | None]:
        """Return the active sliding-window limit and optional retry wait."""
        state = await self._state()
        now = time.time()
        retry_after_until = _float_state(state, "retry_after_until_epoch")
        if retry_after_until and retry_after_until > now:
            return 0, max(retry_after_until - now, 0.05)

        state = await self._maybe_ramp_up(state=state, now=now)
        effective_limit = _int_state(
            state,
            "effective_limit_per_minute",
            self.initial_effective_limit_per_minute,
        )
        target_limit = _int_state(
            state,
            "target_limit_per_minute",
            self.initial_target_limit_per_minute,
        )
        return max(min(effective_limit, target_limit), 1), None

    async def acquire(self, cost: int = 1) -> None:
        while not await self.try_acquire(cost):
            await self.sleeper(self._last_wait_seconds)

    async def current_usage(self) -> int:
        now = time.time()
        await self.redis.zremrangebyscore(self._window_key(), "-inf", now - 60.0)
        return int(await self.redis.zcard(self._window_key()))

    async def record_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
        *,
        request_type: str | None = None,
    ) -> RateLimitBackoffEvent | None:
        if status_code == 429:
            return await self.record_rate_limited(headers, request_type=request_type)
        if status_code < 400:
            await self._maybe_ramp_up(state=await self._state(), now=time.time())
        return None

    async def record_rate_limited(
        self,
        headers: Mapping[str, str],
        *,
        request_type: str | None = None,
    ) -> RateLimitBackoffEvent:
        now = time.time()
        state = await self._state()
        effective_limit = _int_state(
            state,
            "effective_limit_per_minute",
            self.initial_effective_limit_per_minute,
        )
        target_limit = _int_state(
            state,
            "target_limit_per_minute",
            self.initial_target_limit_per_minute,
        )
        last_429_epoch = _float_state(state, "last_429_at_epoch") or 0.0
        previous_count = _int_state(state, "consecutive_429_count", 0)
        consecutive_429s = (
            previous_count + 1
            if last_429_epoch and now - last_429_epoch <= self.isolated_429_window_seconds
            else 1
        )
        has_account_headers = _has_account_rate_limit_headers(headers)
        decrease_step = self._decrease_step_for_429(
            consecutive_429s=consecutive_429s,
            has_account_headers=has_account_headers,
            request_type=request_type,
        )
        next_target_limit = max(
            self.target_min_limit_per_minute,
            target_limit - self.target_decrease_step_per_minute,
        )
        effective_decrease_step = decrease_step if consecutive_429s >= 3 else 0
        next_limit = max(self.min_limit_per_minute, effective_limit - effective_decrease_step)
        header_limit = _int_header(headers, "X-RateLimit-Limit")
        if header_limit:
            next_limit = max(self.min_limit_per_minute, min(next_limit, header_limit))
            next_target_limit = max(
                self.target_min_limit_per_minute,
                min(next_target_limit, header_limit),
            )
        retry_after_seconds = retry_after_delay_seconds(headers, now=now)
        retry_after_until = now + retry_after_seconds if retry_after_seconds else 0.0
        await self.redis.hset(
            self._state_key(),
            mapping={
                "configured_limit_per_minute": self.configured_limit_per_minute,
                "effective_limit_per_minute": next_limit,
                "target_limit_per_minute": next_target_limit,
                "last_429_at_epoch": now,
                "last_ramp_at_epoch": now,
                "last_target_ramp_at_epoch": now,
                "retry_after_until_epoch": retry_after_until,
                "consecutive_429_count": consecutive_429s,
            },
        )
        return RateLimitBackoffEvent(
            old_effective_limit_per_minute=effective_limit,
            new_effective_limit_per_minute=next_limit,
            reason=self._backoff_reason(
                decrease_step=decrease_step,
                has_account_headers=has_account_headers,
                retry_after_seconds=retry_after_seconds,
            ),
            recovery_eta_seconds=self.ramp_idle_seconds if next_limit < effective_limit else 0.0,
            retry_after_seconds=retry_after_seconds,
            consecutive_429s=consecutive_429s,
        )

    def _decrease_step_for_429(
        self,
        *,
        consecutive_429s: int,
        has_account_headers: bool,
        request_type: str | None,
    ) -> int:
        if not has_account_headers and consecutive_429s < 3:
            return 0
        if not has_account_headers:
            return max(1, self.decrease_step_per_minute // 3)
        if request_type == "price_update":
            return max(1, self.decrease_step_per_minute // 3)
        if consecutive_429s <= 1:
            return max(1, self.decrease_step_per_minute // 3)
        if consecutive_429s == 2:
            return max(1, (self.decrease_step_per_minute * 2) // 3)
        return self.decrease_step_per_minute

    def _backoff_reason(
        self,
        *,
        decrease_step: int,
        has_account_headers: bool,
        retry_after_seconds: float | None,
    ) -> str:
        if retry_after_seconds:
            return "retry_after"
        if decrease_step <= 0:
            return "profile_or_endpoint_rate_limited"
        if has_account_headers:
            return "account_rate_limit_headers"
        return "repeated_profile_rate_limited"

    async def snapshot(self) -> RateLimitSnapshot:
        state = await self._state()
        now = time.time()
        effective_limit = _int_state(
            state,
            "effective_limit_per_minute",
            self.initial_effective_limit_per_minute,
        )
        target_limit = _int_state(
            state,
            "target_limit_per_minute",
            self.initial_target_limit_per_minute,
        )
        retry_after_until_epoch = _float_state(state, "retry_after_until_epoch")
        last_429_epoch = _float_state(state, "last_429_at_epoch")
        backoff_active = bool(
            effective_limit < self.configured_limit_per_minute
            or (retry_after_until_epoch and retry_after_until_epoch > now)
        )
        return RateLimitSnapshot(
            configured_limit_per_minute=self.configured_limit_per_minute,
            effective_limit_per_minute=effective_limit,
            current_usage=await self.current_usage(),
            backoff_active=backoff_active,
            last_429_at=_datetime_from_epoch(last_429_epoch),
            retry_after_until=_datetime_from_epoch(retry_after_until_epoch),
            target_limit_per_minute=target_limit,
        )

    async def _state(self) -> dict[str, str]:
        state = await self.redis.hgetall(self._state_key())
        if state:
            mapping: dict[str, object] = {}
            if "configured_limit_per_minute" not in state:
                mapping["configured_limit_per_minute"] = self.configured_limit_per_minute
            if "effective_limit_per_minute" not in state:
                mapping["effective_limit_per_minute"] = self.initial_effective_limit_per_minute
            if "target_limit_per_minute" not in state:
                mapping["target_limit_per_minute"] = self.initial_target_limit_per_minute
            if "last_target_ramp_at_epoch" not in state:
                mapping["last_target_ramp_at_epoch"] = time.time()
            if "last_ramp_at_epoch" not in state:
                mapping["last_ramp_at_epoch"] = time.time()
            if "last_429_at_epoch" not in state:
                mapping["last_429_at_epoch"] = 0.0
            if "retry_after_until_epoch" not in state:
                mapping["retry_after_until_epoch"] = 0.0
            if "consecutive_429_count" not in state:
                mapping["consecutive_429_count"] = 0
            if mapping:
                await self.redis.hset(self._state_key(), mapping=mapping)
                return await self._state()
            return dict(state)
        await self.redis.hset(
            self._state_key(),
            mapping={
                "configured_limit_per_minute": self.configured_limit_per_minute,
                "effective_limit_per_minute": self.initial_effective_limit_per_minute,
                "target_limit_per_minute": self.initial_target_limit_per_minute,
                "last_ramp_at_epoch": time.time(),
                "last_target_ramp_at_epoch": time.time(),
                "last_429_at_epoch": 0.0,
                "retry_after_until_epoch": 0.0,
                "consecutive_429_count": 0,
            },
        )
        return await self._state()

    async def _maybe_ramp_up(self, *, state: dict[str, str], now: float) -> dict[str, str]:
        effective_limit = _int_state(
            state,
            "effective_limit_per_minute",
            self.initial_effective_limit_per_minute,
        )
        target_limit = _int_state(
            state,
            "target_limit_per_minute",
            self.initial_target_limit_per_minute,
        )
        mapping: dict[str, object] = {"configured_limit_per_minute": self.configured_limit_per_minute}

        last_ramp_at = _float_state(state, "last_ramp_at_epoch") or now
        if effective_limit < self.configured_limit_per_minute and now - last_ramp_at >= self.ramp_idle_seconds:
            steps = int((now - last_ramp_at) // self.ramp_idle_seconds)
            effective_limit = min(
                self.configured_limit_per_minute,
                effective_limit + steps * self.ramp_step_per_minute,
            )
            mapping["effective_limit_per_minute"] = effective_limit
            mapping["last_ramp_at_epoch"] = last_ramp_at + steps * self.ramp_idle_seconds
            mapping["consecutive_429_count"] = 0

        last_target_ramp_at = _float_state(state, "last_target_ramp_at_epoch") or now
        if target_limit < self.initial_target_limit_per_minute and now - last_target_ramp_at >= self.target_ramp_idle_seconds:
            steps = int((now - last_target_ramp_at) // self.target_ramp_idle_seconds)
            target_limit = min(
                self.initial_target_limit_per_minute,
                target_limit + steps * self.target_decrease_step_per_minute,
            )
            mapping["target_limit_per_minute"] = target_limit
            mapping["last_target_ramp_at_epoch"] = last_target_ramp_at + steps * self.target_ramp_idle_seconds

        if len(mapping) > 1:
            await self.redis.hset(self._state_key(), mapping=mapping)
            return await self._state()
        return state

    async def _try_sliding_window_acquire(self, limit: int, cost: int) -> bool:
        result = await self.redis.eval(
            SLIDING_WINDOW_SCRIPT,
            2,
            self._window_key(),
            self._seq_key(),
            time.time(),
            60.0,
            max(limit, 1),
            cost,
            130,
        )
        allowed = int(result[0]) == 1
        self._last_wait_seconds = max(float(result[2] or 0.05), 0.05)
        return allowed

    async def _increment_usage(self, cost: int) -> None:
        key = self._usage_key()
        pipe = self.redis.pipeline(transaction=True)
        pipe.incrby(key, cost)
        pipe.expire(key, 125)
        await pipe.execute()


class CompositeRateLimiter:
    """Profile limiter + global limiter + small pacing/backoff guard."""

    def __init__(
        self,
        *,
        profile_limiter: RateLimiter,
        global_limiter: RateLimiter,
        burst_limiter: RateLimiter | None = None,
        account_burst_limiter: RateLimiter | None = None,
        min_delay_ms: int = 0,
        max_delay_ms: int = 5000,
        jitter_ms: int = 0,
        backoff_factor: float = 2.0,
        sleeper=asyncio.sleep,
    ):
        self.profile_limiter = profile_limiter
        self.global_limiter = global_limiter
        self.burst_limiter = burst_limiter
        self.account_burst_limiter = account_burst_limiter
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.jitter_ms = jitter_ms
        self.backoff_factor = backoff_factor
        self.sleeper = sleeper
        self._extra_delay_ms = 0.0
        self._consecutive_errors = 0
        self._last_wait_seconds = 0.0
        self._last_acquire_wait_seconds = 0.0
        self._last_limit_reason = "allowed"

    @property
    def last_wait_seconds(self) -> float:
        return self._last_wait_seconds

    @property
    def last_acquire_wait_seconds(self) -> float:
        return self._last_acquire_wait_seconds

    @property
    def last_limit_reason(self) -> str:
        return self._last_limit_reason

    async def try_acquire(self, cost: int = 1) -> bool:
        result = await self._try_acquire_multi_sliding(cost=cost)
        if result is not None:
            allowed, wait_seconds, reason = result
            self._last_wait_seconds = wait_seconds
            self._last_limit_reason = reason
            return allowed

        if self.account_burst_limiter and not await self.account_burst_limiter.try_acquire(cost):
            self._last_limit_reason = "account_burst"
            return False
        if self.burst_limiter and not await self.burst_limiter.try_acquire(cost):
            self._last_limit_reason = "profile_burst"
            return False
        if not await self.profile_limiter.try_acquire(cost):
            self._last_limit_reason = "profile_window"
            return False
        allowed = await self.global_limiter.try_acquire(cost)
        self._last_limit_reason = "allowed" if allowed else "account_window"
        return allowed

    async def acquire(self, cost: int = 1) -> None:
        waited_seconds = 0.0
        wait_reason = "allowed"
        while not await self.try_acquire(cost):
            wait_seconds = max(self._last_wait_seconds, 0.05)
            waited_seconds += wait_seconds
            wait_reason = self._last_limit_reason
            await self.sleeper(wait_seconds)
        self._last_acquire_wait_seconds = waited_seconds
        if waited_seconds > 0:
            self._last_limit_reason = wait_reason
        await self._sleep_after_request()

    async def acquire_for_request(
        self,
        *,
        cost: int = 1,
        position_amount: int | None = None,
        request_type: str | None = None,
        profile: str | None = None,
    ) -> None:
        waited_seconds = 0.0
        wait_reason = "allowed"
        while not await self.try_acquire_for_request(
            cost=cost,
            position_amount=position_amount,
            request_type=request_type,
            profile=profile,
        ):
            wait_seconds = max(self._last_wait_seconds, 0.05)
            waited_seconds += wait_seconds
            wait_reason = self._last_limit_reason
            await self.sleeper(wait_seconds)
        self._last_acquire_wait_seconds = waited_seconds
        if waited_seconds > 0:
            self._last_limit_reason = wait_reason
        await self._sleep_after_request()

    async def try_acquire_for_request(
        self,
        *,
        cost: int = 1,
        position_amount: int | None = None,
        request_type: str | None = None,
        profile: str | None = None,
    ) -> bool:
        result = await self._try_acquire_multi_sliding(
            cost=cost,
            position_amount=position_amount,
            request_type=request_type,
            profile=profile,
        )
        if result is None:
            return await self.try_acquire(cost)

        allowed, wait_seconds, reason = result
        self._last_wait_seconds = wait_seconds
        self._last_limit_reason = reason
        return allowed

    async def current_usage(self) -> int:
        return await self.profile_limiter.current_usage()

    async def record_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
        *,
        request_type: str | None = None,
    ) -> RateLimitBackoffEvent | None:
        if status_code < 400 and hasattr(self, "reset_backoff"):
            self.reset_backoff()
        elif status_code in {403, 429}:
            self.apply_backoff(str(status_code))

        if hasattr(self.global_limiter, "record_response"):
            try:
                return await self.global_limiter.record_response(
                    status_code,
                    headers,
                    request_type=request_type,
                )
            except TypeError:
                await self.global_limiter.record_response(status_code, headers)
        return None

    async def account_snapshot(self) -> RateLimitSnapshot | None:
        if hasattr(self.global_limiter, "snapshot"):
            return await self.global_limiter.snapshot()
        return None

    async def _try_acquire_multi_sliding(
        self,
        *,
        cost: int = 1,
        position_amount: int | None = None,
        request_type: str | None = None,
        profile: str | None = None,
    ) -> tuple[bool, float, str] | None:
        specs = await self._multi_sliding_specs(
            position_amount=position_amount,
            request_type=request_type,
            profile=profile,
        )
        if specs is None:
            return None

        redis, seq_key, windows = specs
        if not windows:
            return None
        ttl = int(max(window["window_seconds"] for window in windows) * 2 + 10)
        keys = [seq_key, *[str(window["key"]) for window in windows]]
        args: list[object] = [time.time(), cost, ttl, len(windows)]
        for window in windows:
            args.extend([int(window["limit"]), float(window["window_seconds"])])

        result = await redis.eval(MULTI_SLIDING_WINDOW_SCRIPT, len(keys), *keys, *args)
        allowed = int(result[0]) == 1
        wait_seconds = max(float(result[2] or 0.0), 0.0)
        denied_index = int(result[3] or 0)
        reason = "allowed"
        if not allowed and denied_index > 0:
            denied_window = windows[denied_index - 1]
            reason = str(denied_window["reason"])
            wait_seconds = max(wait_seconds, float(denied_window.get("retry_wait_seconds", 0.0)))
        return allowed, wait_seconds, reason

    async def _multi_sliding_specs(
        self,
        *,
        position_amount: int | None,
        request_type: str | None,
        profile: str | None,
    ) -> tuple[Redis, str, list[dict[str, object]]] | None:
        if not isinstance(self.profile_limiter, RedisSlidingWindowRateLimiter):
            return None
        redis = self.profile_limiter.redis
        seq_key = f"{self.profile_limiter.key_prefix}:composite-seq"
        windows: list[dict[str, object]] = []

        if self.account_burst_limiter is not None:
            if not isinstance(self.account_burst_limiter, RedisSlidingWindowRateLimiter):
                return None
            if self.account_burst_limiter.redis is not redis:
                return None
            windows.append(
                {
                    "key": self.account_burst_limiter._window_key(),
                    "limit": max(int(self.account_burst_limiter.limit), 1),
                    "window_seconds": float(self.account_burst_limiter.window_seconds),
                    "reason": "account_burst",
                }
            )

        if self.burst_limiter is not None:
            if not isinstance(self.burst_limiter, RedisSlidingWindowRateLimiter):
                return None
            if self.burst_limiter.redis is not redis:
                return None
            windows.append(
                {
                    "key": self.burst_limiter._window_key(),
                    "limit": max(int(self.burst_limiter.limit), 1),
                    "window_seconds": float(self.burst_limiter.window_seconds),
                    "reason": "profile_burst",
                }
            )

        windows.append(
            {
                "key": self.profile_limiter._window_key(),
                "limit": max(int(self.profile_limiter.limit), 1),
                "window_seconds": float(self.profile_limiter.window_seconds),
                "reason": "profile_window",
            }
        )

        global_window = await self._global_sliding_window_spec(
            position_amount=position_amount,
            request_type=request_type,
            profile=profile,
        )
        if global_window is None:
            return None
        global_redis, global_spec = global_window
        if global_redis is not redis:
            return None
        windows.append(global_spec)
        return redis, seq_key, windows

    async def _global_sliding_window_spec(
        self,
        *,
        position_amount: int | None,
        request_type: str | None,
        profile: str | None,
    ) -> tuple[Redis, dict[str, object]] | None:
        if isinstance(self.global_limiter, RedisAdaptiveTokenBucketRateLimiter):
            limit, retry_wait_seconds = await self.global_limiter.active_limit_state()
            if retry_wait_seconds is not None:
                self._last_wait_seconds = retry_wait_seconds
                self._last_limit_reason = "retry_after"
                return (
                    self.global_limiter.redis,
                    {
                        "key": self.global_limiter._window_key(),
                        "limit": 0,
                        "window_seconds": 60.0,
                        "reason": "retry_after",
                        "retry_wait_seconds": retry_wait_seconds,
                    },
                )
            limit = self._priority_adjusted_account_limit(
                limit,
                position_amount=position_amount,
                request_type=request_type,
                profile=profile,
            )
            return (
                self.global_limiter.redis,
                {
                    "key": self.global_limiter._window_key(),
                    "limit": max(int(limit), 1),
                    "window_seconds": 60.0,
                    "reason": "account_window",
                },
            )
        if isinstance(self.global_limiter, RedisSlidingWindowRateLimiter):
            limit = self._priority_adjusted_account_limit(
                self.global_limiter.limit,
                position_amount=position_amount,
                request_type=request_type,
                profile=profile,
            )
            return (
                self.global_limiter.redis,
                {
                    "key": self.global_limiter._window_key(),
                    "limit": max(int(limit), 1),
                    "window_seconds": float(self.global_limiter.window_seconds),
                    "reason": "account_window",
                },
            )
        return None

    def _priority_adjusted_account_limit(
        self,
        limit: int,
        *,
        position_amount: int | None,
        request_type: str | None,
        profile: str | None,
    ) -> int:
        reserve = _account_reserve_slots(
            profile=profile,
            position_amount=position_amount,
            request_type=request_type,
        )
        if reserve <= 0:
            return limit
        return max(limit - reserve, 1)

    def apply_backoff(self, error_kind: str | None = None) -> float:
        self._consecutive_errors += 1
        if error_kind in {"proxy", "network"}:
            adaptive_delay_ms = transport_backoff_seconds(self._consecutive_errors) * 1000
        else:
            adaptive_delay_ms = adaptive_backoff_seconds(self._consecutive_errors) * 1000
        self._extra_delay_ms = adaptive_delay_ms
        return self._extra_delay_ms / 1000

    def reset_backoff(self) -> None:
        self._extra_delay_ms = 0.0
        self._consecutive_errors = 0

    @property
    def backoff_active(self) -> bool:
        return self._extra_delay_ms > 0

    def set_profile_limit(self, limit: int) -> None:
        if hasattr(self.profile_limiter, "limit"):
            self.profile_limiter.limit = limit

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


def retry_after_delay_seconds(
    headers: Mapping[str, str],
    *,
    now: float | None = None,
) -> float | None:
    current_time = time.time() if now is None else now
    retry_after = _header_value(headers, "Retry-After")
    if retry_after:
        retry_after = retry_after.strip()
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            try:
                retry_dt = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                retry_dt = None
            if retry_dt is not None:
                if retry_dt.tzinfo is None:
                    retry_dt = retry_dt.replace(tzinfo=UTC)
                return max(retry_dt.timestamp() - current_time, 0.0)

    remaining = _header_value(headers, "X-RateLimit-Remaining")
    reset = _header_value(headers, "X-RateLimit-Reset")
    if remaining == "0" and reset:
        try:
            reset_value = float(reset)
        except ValueError:
            return None
        if reset_value > current_time:
            return max(reset_value - current_time, 0.0)
        return max(reset_value, 0.0)
    return None


def _header_value(headers: Mapping[str, str], key: str) -> str | None:
    for header_key, value in headers.items():
        if header_key.lower() == key.lower():
            return value
    return None


def _int_header(headers: Mapping[str, str], key: str) -> int | None:
    value = _header_value(headers, key)
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _has_account_rate_limit_headers(headers: Mapping[str, str]) -> bool:
    return any(
        _header_value(headers, key) is not None
        for key in (
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        )
    )


def _account_reserve_slots(
    *,
    profile: str | None,
    position_amount: int | None,
    request_type: str | None,
) -> int:
    """Hold a few account slots for hot requests when the 60s window is almost full."""
    if position_amount == 500:
        return 0

    normalized_profile = (profile or "").lower().replace("-", "_")
    reserve = 0
    if normalized_profile in {"slow", "proxy_slow", "worker_slow"}:
        reserve = 20
    elif normalized_profile in {"fast_2", "proxy_fast_2", "worker_fast_2"}:
        reserve = 10
    elif normalized_profile in {"fast_1", "proxy_fast_1", "worker_fast_1"}:
        reserve = 5

    if request_type == "price_update":
        return max(reserve - 5, 0)
    return reserve


def _int_state(state: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(state.get(key, default)))
    except (TypeError, ValueError):
        return default


def _float_state(state: dict[str, str], key: str) -> float | None:
    try:
        value = float(state.get(key, 0.0))
    except (TypeError, ValueError):
        return None
    return value or None


def _datetime_from_epoch(value: float | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, UTC)
