import pytest

from app.repricer.rate_limiter import InMemoryFixedWindowRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_does_not_exceed_100_requests_per_minute() -> None:
    limiter = InMemoryFixedWindowRateLimiter(limit=100, window_seconds=60)

    accepted = [await limiter.try_acquire() for _ in range(101)]

    assert accepted.count(True) == 100
    assert accepted[-1] is False

