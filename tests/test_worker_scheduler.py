from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.db.models import Position
from app.repricer.scheduler import RAMP_UP_IDLE_SECONDS, RepricerScheduler, RuntimeScheduleState
from app.repricer.socket_listener import socket_lot_signal_key


class FakeRedis:
    def __init__(self, data: dict[str, str] | None = None):
        self.data = data or {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.data.get(key)

    async def delete(self, key: str):
        self.deleted.append(key)
        self.data.pop(key, None)


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


def test_worker_scheduler_ramps_effective_limit_after_three_minutes_without_429() -> None:
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


@pytest.mark.asyncio
async def test_slow_429_does_not_reduce_fast_1_profile_limit() -> None:
    fast_1 = RepricerScheduler(
        settings=Settings(_env_file=None, worker_group="fast_1"),
        session_factory=object(),
        redis=object(),
    )
    slow = RepricerScheduler(
        settings=Settings(_env_file=None, worker_group="slow"),
        session_factory=object(),
        redis=object(),
    )

    await slow._update_error_state("failed", "rate_limited", position_amount=200)

    assert slow.effective_request_limit_per_minute == 90
    assert fast_1.effective_request_limit_per_minute == 100
    assert fast_1.rate_limiter.profile_limiter.limit == 100


@pytest.mark.asyncio
async def test_fast_2_429_does_not_reduce_fast_1_profile_limit() -> None:
    fast_1 = RepricerScheduler(
        settings=Settings(_env_file=None, worker_group="fast_1"),
        session_factory=object(),
        redis=object(),
    )
    fast_2 = RepricerScheduler(
        settings=Settings(_env_file=None, worker_group="fast_2"),
        session_factory=object(),
        redis=object(),
    )

    await fast_2._update_error_state("failed", "rate_limited", position_amount=1200)

    assert fast_2.effective_request_limit_per_minute == 90
    assert fast_1.effective_request_limit_per_minute == 100
    assert fast_1.rate_limiter.profile_limiter.limit == 100


@pytest.mark.asyncio
async def test_socket_signal_makes_matching_position_due_now() -> None:
    redis = FakeRedis({socket_lot_signal_key("2000"): "1"})
    settings = Settings(
        _env_file=None,
        worker_group="fast_1",
        starvell_socket_enabled=True,
    )
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=redis,  # type: ignore[arg-type]
    )
    scheduler.schedule_runtime[500] = RuntimeScheduleState(
        position_amount=500,
        lot_id="2000",
        proxy_profile="fast_1",
        base_interval_seconds=1.0,
        current_interval_seconds=10.0,
        next_run_monotonic=999999.0,
        last_checked_at=datetime.now(UTC),
        last_competitor_price=None,
        last_own_price=None,
    )

    await scheduler._apply_realtime_signals([Position(robux_amount=500, lot_id="2000")])

    assert scheduler.schedule_runtime[500].next_run_monotonic < 999999.0
    assert scheduler.schedule_runtime[500].delay_reason == "socket_event"
    assert socket_lot_signal_key("2000") in redis.deleted
