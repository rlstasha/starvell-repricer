import pytest

import app.repricer.rate_limiter as rate_limiter_module
from app.repricer.rate_limiter import (
    CompositeRateLimiter,
    InMemoryFixedWindowRateLimiter,
    RedisAdaptiveTokenBucketRateLimiter,
    adaptive_backoff_seconds,
    retry_after_delay_seconds,
    transport_backoff_seconds,
)


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, mapping: dict[str, object]) -> int:
        self.hashes.setdefault(key, {}).update({item_key: str(value) for item_key, value in mapping.items()})
        return len(mapping)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.mark.asyncio
async def test_rate_limiter_does_not_exceed_100_requests_per_minute() -> None:
    limiter = InMemoryFixedWindowRateLimiter(limit=100, window_seconds=60)

    accepted = [await limiter.try_acquire() for _ in range(101)]

    assert accepted.count(True) == 100
    assert accepted[-1] is False


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
    assert event.old_effective_limit_per_minute == 270
    assert event.new_effective_limit_per_minute == 240
    assert snapshot.effective_limit_per_minute == 240


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
    assert (await limiter.snapshot()).effective_limit_per_minute == 290

    now[0] += 181
    await limiter.record_response(200, {})

    assert (await limiter.snapshot()).effective_limit_per_minute == 300
