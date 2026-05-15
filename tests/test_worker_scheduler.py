import pytest

from app.core.config import Settings
from app.db.models import Position
from app.repricer.scheduler import RAMP_UP_IDLE_SECONDS, RepricerScheduler


def test_worker_scheduler_filters_fast_2_positions_only() -> None:
    settings = Settings(_env_file=None, worker_group="fast_2")
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=object(),
    )
    positions = [
        Position(robux_amount=400),
        Position(robux_amount=500),
        Position(robux_amount=1200),
        Position(robux_amount=1700),
        Position(robux_amount=2000),
        Position(robux_amount=22500),
    ]

    filtered = scheduler._filter_assigned_positions(positions)

    assert [position.robux_amount for position in filtered] == [400, 1200, 1700, 2000]


@pytest.mark.asyncio
async def test_worker_scheduler_reduces_effective_limit_after_429() -> None:
    settings = Settings(_env_file=None, worker_group="fast_1")
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=object(),
    )

    await scheduler._update_error_state("failed", "rate_limited")

    assert scheduler.effective_request_limit_per_minute == 90
    assert scheduler.rate_limiter.profile_limiter.limit == 90
    assert scheduler.last_429_at is not None
    assert scheduler._backoff_active() is True


def test_worker_scheduler_ramps_effective_limit_after_ten_minutes_without_429() -> None:
    settings = Settings(_env_file=None, worker_group="fast_1")
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=object(),
    )
    scheduler._set_effective_request_limit(80)
    scheduler.last_limit_ramp_monotonic = 100.0

    changed = scheduler._maybe_ramp_up_limit(now=100.0 + RAMP_UP_IDLE_SECONDS + 1)

    assert changed is True
    assert scheduler.effective_request_limit_per_minute == 90
    assert scheduler.rate_limiter.profile_limiter.limit == 90
