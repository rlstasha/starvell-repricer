from decimal import Decimal

import pytest

from app.repricer.adaptive_scheduler import (
    ULTRA_FAST_POSITION_AMOUNT,
    _clear_interval_override_cache,
    _interval_override_value,
    apply_strategy_activity_floor,
    choose_dynamic_delay,
    display_interval_range,
    is_active_strategy_reason,
    market_activity_label,
    timing_for_position,
    update_change_score,
)


@pytest.fixture(autouse=True)
def clear_interval_override_cache() -> None:
    _clear_interval_override_cache()
    yield
    _clear_interval_override_cache()


def test_interval_override_value_reads_env_once(monkeypatch) -> None:
    calls = []

    def fake_getenv(name: str) -> str | None:
        calls.append(name)
        return "0.4"

    monkeypatch.setattr("app.repricer.adaptive_scheduler.os.getenv", fake_getenv)

    assert _interval_override_value("ULTRA_FAST_MIN_INTERVAL_SECONDS") == 0.4
    assert _interval_override_value("ULTRA_FAST_MIN_INTERVAL_SECONDS") == 0.4
    assert calls == ["ULTRA_FAST_MIN_INTERVAL_SECONDS"]


def test_interval_override_value_reads_settings_once(monkeypatch) -> None:
    calls = []

    class FakeSettings:
        fast1_min_interval_seconds = 1.1

    def fake_get_settings() -> FakeSettings:
        calls.append("settings")
        return FakeSettings()

    monkeypatch.setattr("app.repricer.adaptive_scheduler.os.getenv", lambda name: None)
    monkeypatch.setattr("app.core.config.get_settings", fake_get_settings)

    assert _interval_override_value("FAST1_MIN_INTERVAL_SECONDS") == 1.1
    assert _interval_override_value("FAST1_MIN_INTERVAL_SECONDS") == 1.1
    assert calls == ["settings"]


def test_change_score_uses_exponential_smoothing() -> None:
    score = update_change_score(0.5, Decimal("100"), Decimal("110"))

    assert round(score, 2) == 0.65


def test_dynamic_delay_uses_backoff_range_after_429() -> None:
    decision = choose_dynamic_delay(
        worker_group="fast_1",
        change_score=0.5,
        backoff_active=True,
        random_uniform=lambda low, high: (low + high) / 2,
    )

    assert decision.reason == "backoff_after_429"
    assert decision.range_min_seconds == 2.5
    assert decision.range_max_seconds == 4.0
    assert 2.5 <= decision.delay_seconds <= 4.0


def test_dynamic_delay_avoids_identical_repeats() -> None:
    decision = choose_dynamic_delay(
        worker_group="fast_2",
        change_score=0.5,
        previous_delay_seconds=2.4,
        random_uniform=lambda low, high: 2.4,
    )

    assert decision.delay_seconds != 2.4


def test_display_ranges_match_proxy_profiles() -> None:
    assert display_interval_range("fast_1") == (0.8, 2.2)
    assert display_interval_range("fast_2") == (2.0, 3.0)
    assert display_interval_range("slow") == (4.5, 6.5)


def test_fast_profiles_stay_capped_when_market_is_calm() -> None:
    fast_1 = choose_dynamic_delay(
        worker_group="fast_1",
        change_score=0.0,
        random_uniform=lambda low, high: high,
    )
    fast_2 = choose_dynamic_delay(
        worker_group="fast_2",
        change_score=0.0,
        random_uniform=lambda low, high: high,
    )

    assert fast_1.reason == "market_calm"
    assert fast_1.delay_seconds <= 2.7
    assert fast_2.reason == "market_calm"
    assert fast_2.delay_seconds <= 3.0


def test_500_position_uses_ultrafast_timing_inside_fast_1() -> None:
    timing = timing_for_position("fast_1", ULTRA_FAST_POSITION_AMOUNT)
    decision = choose_dynamic_delay(
        worker_group="fast_1",
        position_amount=ULTRA_FAST_POSITION_AMOUNT,
        change_score=0.5,
        random_uniform=lambda low, high: high,
    )

    assert timing.min_seconds == 0.4
    assert timing.max_seconds == 1.3
    assert decision.delay_seconds <= 1.3
    assert display_interval_range("fast_1", position_amount=500) == (0.4, 1.3)


def test_fast_interval_minimum_can_be_overridden_from_env(monkeypatch) -> None:
    monkeypatch.setenv("FAST1_MIN_INTERVAL_SECONDS", "1.2")

    timing = timing_for_position("fast_1", 800)

    assert timing.min_seconds == 1.2
    assert timing.normal_min_seconds == 1.2
    assert display_interval_range("fast_1") == (1.2, 2.2)


def test_fast2_interval_minimum_can_be_overridden_from_env(monkeypatch) -> None:
    monkeypatch.setenv("FAST2_MIN_INTERVAL_SECONDS", "1.7")

    timing = timing_for_position("fast_2", 1200)

    assert timing.min_seconds == 1.7
    assert timing.normal_min_seconds == 1.7
    assert display_interval_range("fast_2") == (1.7, 3.0)


def test_slow_interval_range_can_be_overridden_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SLOW_MIN_INTERVAL_SECONDS", "9.0")
    monkeypatch.setenv("SLOW_MAX_INTERVAL_SECONDS", "15.0")

    timing = timing_for_position("slow", 2100)

    assert timing.min_seconds == 9.0
    assert timing.max_seconds == 15.0
    assert timing.normal_min_seconds == 9.0
    assert timing.normal_max_seconds == 15.0
    assert display_interval_range("slow") == (9.0, 15.0)


def test_ultrafast_interval_minimum_can_be_overridden_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ULTRA_FAST_MIN_INTERVAL_SECONDS", "0.6")

    timing = timing_for_position("fast_1", ULTRA_FAST_POSITION_AMOUNT)

    assert timing.min_seconds == 0.6
    assert timing.normal_min_seconds == 0.6
    assert display_interval_range("fast_1", position_amount=500) == (0.6, 1.3)


def test_min_price_bounce_reason_keeps_scheduler_active() -> None:
    score = update_change_score(0.2, Decimal("90"), Decimal("90"))
    score = apply_strategy_activity_floor(score, "min_price_bounce_to_upper_competitor")
    decision = choose_dynamic_delay(
        worker_group="fast_1",
        change_score=score,
        random_uniform=lambda low, high: high,
    )

    assert is_active_strategy_reason("min_price_bounce_to_upper_competitor") is True
    assert is_active_strategy_reason("dry_run_would_update:min_price_bounce_to_upper_competitor") is True
    assert score == 0.65
    assert decision.reason == "normal"


def test_normal_skipped_result_can_still_become_market_calm() -> None:
    score = update_change_score(0.2, Decimal("90"), Decimal("90"))
    score = apply_strategy_activity_floor(score, "already_at_target")
    decision = choose_dynamic_delay(
        worker_group="fast_1",
        change_score=score,
        random_uniform=lambda low, high: high,
    )

    assert score < 0.3
    assert decision.reason == "market_calm"


def test_backoff_still_overrides_min_price_bounce_activity() -> None:
    score = apply_strategy_activity_floor(0.0, "all_competitors_below_min_price")
    decision = choose_dynamic_delay(
        worker_group="fast_1",
        change_score=score,
        backoff_active=True,
        random_uniform=lambda low, high: low,
    )

    assert decision.reason == "backoff_after_429"
    assert decision.delay_seconds >= 2.5


def test_500_position_stays_ultrafast_with_min_price_bounce_activity() -> None:
    score = apply_strategy_activity_floor(0.0, "competitor_above_min_but_step_hits_min")
    decision = choose_dynamic_delay(
        worker_group="fast_1",
        position_amount=ULTRA_FAST_POSITION_AMOUNT,
        change_score=score,
        random_uniform=lambda low, high: high,
    )

    assert decision.reason == "normal"
    assert 0.8 <= decision.delay_seconds <= 1.3


def test_market_activity_labels_are_human_readable() -> None:
    assert market_activity_label(0.9) == "высокая"
    assert market_activity_label(0.5) == "средняя"
    assert market_activity_label(0.1) == "низкая"
