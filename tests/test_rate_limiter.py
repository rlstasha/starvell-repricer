import pytest

import app.repricer.rate_limiter as rate_limiter_module
from app.repricer.rate_limiter import (
    CompositeRateLimiter,
    InMemoryFixedWindowRateLimiter,
    RedisAdaptiveTokenBucketRateLimiter,
    RedisSlidingWindowRateLimiter,
    adaptive_backoff_seconds,
    retry_after_delay_seconds,
    transport_backoff_seconds,
)


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}
        self.zsets: dict[str, list[tuple[float, str]]] = {}
        self.sequences: dict[str, int] = {}

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, mapping: dict[str, object]) -> int:
        self.hashes.setdefault(key, {}).update({item_key: str(value) for item_key, value in mapping.items()})
        return len(mapping)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def zremrangebyscore(self, key: str, minimum: object, maximum: object) -> int:
        max_score = float(maximum)
        existing = self.zsets.get(key, [])
        kept = [(score, member) for score, member in existing if score > max_score]
        removed = len(existing) - len(kept)
        self.zsets[key] = kept
        return removed

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, []))

    async def eval(self, script: str, numkeys: int, *args: object) -> list[object]:
        if "windows_count" in script:
            return self._eval_multi_sliding_window(numkeys, *args)
        return self._eval_sliding_window(numkeys, *args)

    def _eval_sliding_window(self, numkeys: int, *args: object) -> list[object]:
        window_key = str(args[0])
        seq_key = str(args[1])
        now = float(args[2])
        window_seconds = float(args[3])
        limit = int(args[4])
        cost = int(args[5])

        current = self._cleanup_zset(window_key, now, window_seconds)
        if current + cost <= limit:
            self._add_zset_events(window_key, seq_key, now, cost)
            return [1, current + cost, 0]
        wait_seconds = self._oldest_wait_seconds(window_key, now, window_seconds)
        return [0, current, wait_seconds]

    def _eval_multi_sliding_window(self, numkeys: int, *args: object) -> list[object]:
        keys = [str(item) for item in args[:numkeys]]
        argv = list(args[numkeys:])
        seq_key = keys[0]
        now = float(argv[0])
        cost = int(argv[1])
        windows_count = int(argv[3])
        denied_index = 0
        denied_current = 0
        max_wait_seconds = 0.0

        for index in range(windows_count):
            window_key = keys[index + 1]
            limit = int(argv[4 + index * 2])
            window_seconds = float(argv[5 + index * 2])
            current = self._cleanup_zset(window_key, now, window_seconds)
            if current + cost > limit:
                wait_seconds = self._oldest_wait_seconds(window_key, now, window_seconds)
                if wait_seconds > max_wait_seconds:
                    max_wait_seconds = wait_seconds
                    denied_index = index + 1
                    denied_current = current

        if denied_index:
            return [0, denied_current, max_wait_seconds, denied_index]

        for index in range(windows_count):
            self._add_zset_events(keys[index + 1], seq_key, now, cost)
        return [1, 0, 0, 0]

    def _cleanup_zset(self, key: str, now: float, window_seconds: float) -> int:
        cutoff = now - window_seconds
        self.zsets[key] = [(score, member) for score, member in self.zsets.get(key, []) if score > cutoff]
        return len(self.zsets[key])

    def _add_zset_events(self, key: str, seq_key: str, now: float, cost: int) -> None:
        for _ in range(cost):
            self.sequences[seq_key] = self.sequences.get(seq_key, 0) + 1
            self.zsets.setdefault(key, []).append((now, f"{now}:{self.sequences[seq_key]}"))

    def _oldest_wait_seconds(self, key: str, now: float, window_seconds: float) -> float:
        values = self.zsets.get(key, [])
        if not values:
            return 0.05
        oldest = min(score for score, _ in values)
        return max((oldest + window_seconds) - now, 0.05)


@pytest.mark.asyncio
async def test_rate_limiter_does_not_exceed_100_requests_per_minute() -> None:
    limiter = InMemoryFixedWindowRateLimiter(limit=100, window_seconds=60)

    accepted = [await limiter.try_acquire() for _ in range(101)]

    assert accepted.count(True) == 100
    assert accepted[-1] is False


@pytest.mark.asyncio
async def test_redis_sliding_window_counts_last_60_seconds(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    limiter = RedisSlidingWindowRateLimiter(FakeRedis(), limit=3, window_seconds=60)

    assert await limiter.try_acquire(cost=3) is True
    assert await limiter.try_acquire() is False

    now[0] += 60.1

    assert await limiter.try_acquire() is True


@pytest.mark.asyncio
async def test_composite_limiter_acquires_windows_atomically(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    redis = FakeRedis()
    limiter = CompositeRateLimiter(
        profile_limiter=RedisSlidingWindowRateLimiter(redis, limit=300, key_prefix="profile"),
        global_limiter=RedisAdaptiveTokenBucketRateLimiter(
            redis,
            configured_limit_per_minute=300,
            initial_effective_limit_per_minute=300,
            target_limit_per_minute=295,
            key_prefix="account",
        ),
        burst_limiter=RedisSlidingWindowRateLimiter(redis, limit=300, window_seconds=1, key_prefix="burst"),
        account_burst_limiter=RedisSlidingWindowRateLimiter(
            redis,
            limit=300,
            window_seconds=2,
            key_prefix="account-burst",
        ),
    )

    assert await limiter.try_acquire_for_request(cost=295, position_amount=500, profile="fast_1") is True
    assert await limiter.try_acquire_for_request(position_amount=500, profile="fast_1") is False

    assert len(redis.zsets["profile:events"]) == 295
    assert len(redis.zsets["account:events"]) == 295


@pytest.mark.asyncio
async def test_account_reserve_keeps_slots_for_500(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    redis = FakeRedis()
    limiter = CompositeRateLimiter(
        profile_limiter=RedisSlidingWindowRateLimiter(redis, limit=300, key_prefix="profile"),
        global_limiter=RedisAdaptiveTokenBucketRateLimiter(
            redis,
            configured_limit_per_minute=300,
            initial_effective_limit_per_minute=300,
            target_limit_per_minute=295,
            key_prefix="account",
        ),
    )

    assert await limiter.try_acquire_for_request(cost=285, position_amount=500, profile="fast_1") is True
    assert await limiter.try_acquire_for_request(position_amount=200, profile="slow") is False
    assert await limiter.try_acquire_for_request(position_amount=500, profile="fast_1") is True


@pytest.mark.asyncio
async def test_isolated_429_only_lowers_predictive_target(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    limiter = RedisAdaptiveTokenBucketRateLimiter(
        FakeRedis(),
        configured_limit_per_minute=300,
        initial_effective_limit_per_minute=300,
        target_limit_per_minute=295,
        target_min_limit_per_minute=260,
        target_decrease_step_per_minute=10,
        target_ramp_idle_seconds=300,
        decrease_step_per_minute=30,
        ramp_step_per_minute=20,
        ramp_idle_seconds=180,
    )

    event = await limiter.record_response(429, {})

    assert event is not None
    assert event.old_effective_limit_per_minute == 300
    assert event.new_effective_limit_per_minute == 300
    assert (await limiter._state())["target_limit_per_minute"] == "285"

    now[0] += 301
    await limiter.record_response(200, {})

    assert (await limiter._state())["target_limit_per_minute"] == "295"


def test_adaptive_backoff_steps_are_human_scale() -> None:
    assert [adaptive_backoff_seconds(index) for index in range(1, 7)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        15.0,
        15.0,
    ]


def test_transport_backoff_steps_are_shorter_than_rate_limit_backoff() -> None:
    assert [transport_backoff_seconds(index) for index in range(1, 7)] == [
        0.2,
        0.5,
        1.0,
        2.0,
        3.0,
        3.0,
    ]


@pytest.mark.asyncio
async def test_composite_limiter_resets_backoff_after_success() -> None:
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)

    limiter = CompositeRateLimiter(
        profile_limiter=InMemoryFixedWindowRateLimiter(limit=100),
        global_limiter=InMemoryFixedWindowRateLimiter(limit=100),
        min_delay_ms=0,
        jitter_ms=0,
        sleeper=sleeper,
    )

    assert limiter.apply_backoff("429") == 1.0
    assert limiter.apply_backoff("429") == 2.0
    await limiter.acquire()
    assert slept == [2.0]

    limiter.reset_backoff()
    await limiter.acquire()
    assert slept == [2.0]


@pytest.mark.asyncio
async def test_composite_limiter_uses_short_proxy_transport_backoff() -> None:
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)

    limiter = CompositeRateLimiter(
        profile_limiter=InMemoryFixedWindowRateLimiter(limit=100),
        global_limiter=InMemoryFixedWindowRateLimiter(limit=100),
        min_delay_ms=0,
        jitter_ms=0,
        sleeper=sleeper,
    )

    assert limiter.apply_backoff("proxy") == 0.2
    assert limiter.apply_backoff("proxy") == 0.5
    await limiter.acquire()
    assert slept == [0.5]


@pytest.mark.asyncio
async def test_composite_limiter_can_guard_global_bursts() -> None:
    limiter = CompositeRateLimiter(
        profile_limiter=InMemoryFixedWindowRateLimiter(limit=100),
        global_limiter=InMemoryFixedWindowRateLimiter(limit=300),
        burst_limiter=InMemoryFixedWindowRateLimiter(limit=5, window_seconds=1),
        account_burst_limiter=InMemoryFixedWindowRateLimiter(limit=2, window_seconds=1),
    )

    assert await limiter.try_acquire() is True
    assert await limiter.try_acquire() is True
    assert await limiter.try_acquire() is False


def test_retry_after_header_seconds_are_parsed() -> None:
    assert retry_after_delay_seconds({"Retry-After": "8"}, now=100.0) == 8.0


def test_x_rate_limit_reset_is_used_when_remaining_is_zero() -> None:
    assert retry_after_delay_seconds(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "130"},
        now=100.0,
    ) == 30.0


@pytest.mark.asyncio
async def test_account_limiter_ignores_isolated_429_without_account_headers(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    limiter = RedisAdaptiveTokenBucketRateLimiter(
        FakeRedis(),
        configured_limit_per_minute=300,
        initial_effective_limit_per_minute=300,
        decrease_step_per_minute=30,
        ramp_step_per_minute=20,
        ramp_idle_seconds=180,
    )

    event = await limiter.record_response(429, {})
    snapshot = await limiter.snapshot()

    assert event is not None
    assert event.old_effective_limit_per_minute == 300
    assert event.new_effective_limit_per_minute == 300
    assert event.reason == "profile_or_endpoint_rate_limited"
    assert snapshot.effective_limit_per_minute == 300
    assert (await limiter._state())["target_limit_per_minute"] == "290"


@pytest.mark.asyncio
async def test_account_limiter_softly_reduces_after_repeated_429_without_headers(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    limiter = RedisAdaptiveTokenBucketRateLimiter(
        FakeRedis(),
        configured_limit_per_minute=300,
        initial_effective_limit_per_minute=300,
        decrease_step_per_minute=30,
        ramp_step_per_minute=20,
        ramp_idle_seconds=180,
    )

    await limiter.record_response(429, {})
    now[0] += 10
    await limiter.record_response(429, {})
    now[0] += 10
    event = await limiter.record_response(429, {})
    snapshot = await limiter.snapshot()

    assert event is not None
    assert event.reason == "repeated_profile_rate_limited"
    assert event.old_effective_limit_per_minute == 300
    assert event.new_effective_limit_per_minute == 290
    assert snapshot.effective_limit_per_minute == 290


@pytest.mark.asyncio
async def test_account_limiter_repeated_account_headers_still_protect_system(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    limiter = RedisAdaptiveTokenBucketRateLimiter(
        FakeRedis(),
        configured_limit_per_minute=300,
        initial_effective_limit_per_minute=300,
        decrease_step_per_minute=30,
        ramp_step_per_minute=20,
        ramp_idle_seconds=180,
    )

    headers = {"X-RateLimit-Limit": "300"}
    await limiter.record_response(429, headers)
    now[0] += 10
    await limiter.record_response(429, headers)
    now[0] += 10
    event = await limiter.record_response(429, headers)
    snapshot = await limiter.snapshot()

    assert event is not None
    assert event.old_effective_limit_per_minute == 300
    assert event.new_effective_limit_per_minute == 270
    assert snapshot.effective_limit_per_minute == 270


@pytest.mark.asyncio
async def test_account_limiter_recovers_after_three_clean_minutes(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "time", lambda: now[0])
    limiter = RedisAdaptiveTokenBucketRateLimiter(
        FakeRedis(),
        configured_limit_per_minute=300,
        initial_effective_limit_per_minute=300,
        decrease_step_per_minute=30,
        ramp_step_per_minute=20,
        ramp_idle_seconds=180,
    )

    await limiter.record_response(429, {"X-RateLimit-Limit": "300"})
    assert (await limiter.snapshot()).effective_limit_per_minute == 300
    assert (await limiter._state())["target_limit_per_minute"] == "290"

    now[0] += 301
    await limiter.record_response(200, {})

    assert (await limiter.snapshot()).effective_limit_per_minute == 300
    assert (await limiter._state())["target_limit_per_minute"] == "300"
