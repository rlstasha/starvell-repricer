import pytest

from app.repricer.rate_limiter import (
    CompositeRateLimiter,
    InMemoryFixedWindowRateLimiter,
    adaptive_backoff_seconds,
    retry_after_delay_seconds,
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


def test_retry_after_header_seconds_are_parsed() -> None:
    assert retry_after_delay_seconds({"Retry-After": "8"}, now=100.0) == 8.0


def test_x_rate_limit_reset_is_used_when_remaining_is_zero() -> None:
    assert retry_after_delay_seconds(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "130"},
        now=100.0,
    ) == 30.0
