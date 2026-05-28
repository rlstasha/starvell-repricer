import time
from decimal import Decimal

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


def test_worker_scheduler_uses_account_wide_burst_guard_in_token_mode() -> None:
    settings = Settings(
        _env_file=None,
        worker_group="fast_1",
        token_limit_mode=True,
    )
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=object(),
        redis=object(),
    )

    assert scheduler.rate_limiter.burst_limiter is not None
    assert scheduler.rate_limiter.burst_limiter.key_prefix == "repricer:burst:account"


def test_hot_mode_keeps_500_fast_after_success(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: low)
    settings = Settings(_env_file=None, worker_group="fast_1", hot_mode_enabled=True)
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.schedule_runtime = {
        500: RuntimeScheduleState(
            position_amount=500,
            lot_id="2000",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("315.00"),
            last_own_price=Decimal("314.40"),
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=500),
        _SchedulerResult(
            status="success",
            reason="competitor_undercut",
            old_price=Decimal("314.40"),
            new_price=Decimal("314.20"),
            competitor_price=Decimal("315.00"),
        ),
    )

    assert state.delay_reason == "hot_mode_active"
    assert state.current_interval_seconds == 0.25
    assert state.interval_min_seconds == 0.25
    assert state.interval_max_seconds == 0.5


def test_hot_mode_cools_500_after_repeated_target_skips(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: low)
    settings = Settings(_env_file=None, worker_group="fast_1", hot_mode_enabled=True)
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.schedule_runtime = {
        500: RuntimeScheduleState(
            position_amount=500,
            lot_id="2000",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=0.3,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("315.00"),
            last_own_price=Decimal("314.40"),
            hot_consecutive_target_skips=2,
            hot_last_update_monotonic=time.monotonic(),
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=500),
        _SchedulerResult(
            status="skipped",
            reason="min_price_bounce_to_upper_competitor",
            old_price=Decimal("314.40"),
            new_price=Decimal("314.40"),
            competitor_price=Decimal("315.00"),
        ),
    )

    assert state.hot_consecutive_target_skips == 3
    assert state.delay_reason == "hot_mode_cooldown"
    assert state.current_interval_seconds == 0.8


def test_hot_mode_returns_to_fast_when_competitor_changes(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: low)
    settings = Settings(_env_file=None, worker_group="fast_1", hot_mode_enabled=True)
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.schedule_runtime = {
        500: RuntimeScheduleState(
            position_amount=500,
            lot_id="2000",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("315.00"),
            last_own_price=Decimal("314.40"),
            hot_consecutive_target_skips=5,
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=500),
        _SchedulerResult(
            status="skipped",
            reason="min_price_bounce_to_upper_competitor",
            old_price=Decimal("314.40"),
            new_price=Decimal("314.40"),
            competitor_price=Decimal("316.00"),
        ),
    )

    assert state.hot_consecutive_target_skips == 0
    assert state.delay_reason == "hot_mode_active"
    assert state.current_interval_seconds == 0.25


def test_fast_mode_can_speed_active_fast_positions(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: low)
    settings = Settings(_env_file=None, worker_group="fast_1", fast_mode_enabled=True)
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.schedule_runtime = {
        800: RuntimeScheduleState(
            position_amount=800,
            lot_id="2002",
            proxy_profile="fast_1",
            base_interval_seconds=1.8,
            current_interval_seconds=1.5,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("528.20"),
            last_own_price=Decimal("528.10"),
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=800),
        _SchedulerResult(
            status="success",
            reason="competitor_undercut",
            old_price=Decimal("528.10"),
            new_price=Decimal("528.00"),
            competitor_price=Decimal("528.20"),
        ),
    )

    assert state.delay_reason == "fast_mode_active"
    assert state.current_interval_seconds == 0.8


def test_hot_mode_does_not_override_backoff(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: low)
    settings = Settings(_env_file=None, worker_group="fast_1", hot_mode_enabled=True)
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.safe_mode_until = time.monotonic() + 10
    scheduler.schedule_runtime = {
        500: RuntimeScheduleState(
            position_amount=500,
            lot_id="2000",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("315.00"),
            last_own_price=Decimal("314.40"),
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=500),
        _SchedulerResult(
            status="success",
            reason="competitor_undercut",
            old_price=Decimal("314.40"),
            new_price=Decimal("314.20"),
            competitor_price=Decimal("315.00"),
        ),
    )

    assert state.delay_reason == "backoff_after_error"
    assert state.current_interval_seconds >= 2.0


def test_post_update_multiplier_can_schedule_immediate_followup(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: high)
    settings = Settings(
        _env_file=None,
        worker_group="fast_1",
        post_update_interval_multiplier=0.5,
    )
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.schedule_runtime = {
        500: RuntimeScheduleState(
            position_amount=500,
            lot_id="2000",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("315.00"),
            last_own_price=Decimal("314.40"),
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=500),
        _SchedulerResult(
            status="success",
            reason="competitor_undercut",
            old_price=Decimal("314.40"),
            new_price=Decimal("314.20"),
            competitor_price=Decimal("315.00"),
        ),
    )

    assert state.delay_reason == "post_update_followup"
    assert state.current_interval_seconds == 0.2
    assert state.interval_min_seconds == 0.2
    assert state.interval_max_seconds == 0.2


def test_post_update_multiplier_keeps_default_scheduler_behavior(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: high)
    settings = Settings(_env_file=None, worker_group="fast_1")
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.schedule_runtime = {
        500: RuntimeScheduleState(
            position_amount=500,
            lot_id="2000",
            proxy_profile="fast_1",
            base_interval_seconds=1.0,
            current_interval_seconds=1.0,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("315.00"),
            last_own_price=Decimal("314.40"),
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=500),
        _SchedulerResult(
            status="success",
            reason="competitor_undercut",
            old_price=Decimal("314.40"),
            new_price=Decimal("314.20"),
            competitor_price=Decimal("315.00"),
        ),
    )

    assert state.delay_reason != "post_update_followup"
    assert state.current_interval_seconds >= settings.ultra_fast_min_interval_seconds


def test_post_update_multiplier_is_scoped_to_configured_positions(monkeypatch) -> None:
    monkeypatch.setattr("app.repricer.scheduler.random.uniform", lambda low, high: high)
    settings = Settings(
        _env_file=None,
        worker_group="fast_2",
        post_update_interval_multiplier=0.5,
        post_update_interval_positions="500",
    )
    scheduler = RepricerScheduler(settings=settings, session_factory=object(), redis=object())
    scheduler.schedule_runtime = {
        800: RuntimeScheduleState(
            position_amount=800,
            lot_id="2002",
            proxy_profile="fast_2",
            base_interval_seconds=2.4,
            current_interval_seconds=2.4,
            next_run_monotonic=1.0,
            last_checked_at=None,
            last_competitor_price=Decimal("528.20"),
            last_own_price=Decimal("528.10"),
        )
    }

    state = scheduler._update_schedule_after_result(
        Position(robux_amount=800),
        _SchedulerResult(
            status="success",
            reason="competitor_undercut",
            old_price=Decimal("528.10"),
            new_price=Decimal("528.00"),
            competitor_price=Decimal("528.20"),
        ),
    )

    assert state.delay_reason != "post_update_followup"


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


class _SchedulerResult:
    def __init__(
        self,
        *,
        status: str,
        reason: str,
        old_price: Decimal | None,
        new_price: Decimal | None,
        competitor_price: Decimal | None,
    ):
        self.position_amount = 0
        self.status = status
        self.reason = reason
        self.old_price = old_price
        self.new_price = new_price
        self.competitor_price = competitor_price
