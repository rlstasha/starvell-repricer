import pytest

from app.core.config import Settings
from app.db.models import Position
from app.repricer.rate_limiter import NoopRateLimiter
from app.repricer.scheduler import RAMP_UP_IDLE_SECONDS, RepricerScheduler, RuntimeScheduleState


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


def test_worker_scheduler_collects_due_positions_with_concurrency_limit() -> None:
    settings = Settings(_env_file=None, worker_group="fast_1", scheduler_max_concurrent_positions=2)
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=object(),
    )
    scheduler.schedule_runtime = {
        500: RuntimeScheduleState(
            position_amount=500,
            lot_id="2000",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=None,
            last_own_price=None,
        ),
        800: RuntimeScheduleState(
            position_amount=800,
            lot_id="2002",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=None,
            last_own_price=None,
        ),
        1000: RuntimeScheduleState(
            position_amount=1000,
            lot_id="2003",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=None,
            last_own_price=None,
        ),
    }
    scheduler._rebuild_schedule_heap()

    due = scheduler._due_positions(
        {
            500: Position(robux_amount=500),
            800: Position(robux_amount=800),
            1000: Position(robux_amount=1000),
        },
        limit=settings.scheduler_max_concurrent_positions,
    )

    assert len(due) == 2
    assert {position.robux_amount for position in due} <= {500, 800, 1000}
    assert len(scheduler.schedule_heap) == 1


def test_worker_scheduler_can_disable_proactive_rate_limits_for_benchmarks() -> None:
    settings = Settings(
        _env_file=None,
        worker_group="fast_1",
        rate_limiter_soft_cap_enabled=False,
    )
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=object(),
    )

    assert isinstance(scheduler.rate_limiter.profile_limiter, NoopRateLimiter)
    assert isinstance(scheduler.rate_limiter.global_limiter, NoopRateLimiter)
    assert scheduler.rate_limiter.burst_limiter is None


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
