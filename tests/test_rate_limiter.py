import pytest

from app.repricer.rate_limiter import (
    CompositeRateLimiter,
    InMemoryFixedWindowRateLimiter,
    RedisAdaptiveTokenBucketRateLimiter,
    adaptive_backoff_seconds,
    retry_after_delay_seconds,
    transport_backoff_seconds,
)


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


def test_adaptive_limiter_ramp_recovery_eta_uses_configured_step_and_idle() -> None:
    limiter = RedisAdaptiveTokenBucketRateLimiter(
        redis=object(),
        configured_limit_per_minute=700,
        initial_effective_limit_per_minute=700,
        ramp_step_per_minute=30,
        ramp_idle_seconds=60,
    )

    eta = limiter._ramp_recovery_eta_seconds(
        state={"last_ramp_at_epoch": "100"},
        now=100,
        effective_limit=640,
        retry_after_until_epoch=None,
    )

    assert eta == 120.0


@pytest.mark.asyncio
async def test_composite_limiter_exposes_account_and_profile_window_diagnostics() -> None:
    class FakeWindowLimiter:
        def __init__(self, base: int) -> None:
            self.base = base

        async def acquire(self, cost: int = 1) -> None:
            return None

        async def try_acquire(self, cost: int = 1) -> bool:
            return True

        async def current_usage(self) -> int:
            return self.base + 60

        async def usage_in_window(self, window_seconds: int) -> int:
            return self.base + window_seconds

    limiter = CompositeRateLimiter(
        profile_limiter=FakeWindowLimiter(100),
        global_limiter=FakeWindowLimiter(200),
        min_delay_ms=0,
        jitter_ms=0,
    )

    diagnostics = await limiter.usage_diagnostics((5, 60))

    assert diagnostics == {
        "requests_last_5s": 205,
        "profile_requests_last_5s": 105,
        "account_requests_last_5s": 205,
        "requests_last_60s": 260,
        "profile_requests_last_60s": 160,
        "account_requests_last_60s": 260,
    }
