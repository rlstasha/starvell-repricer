from decimal import Decimal

from app.repricer.adaptive_scheduler import (
    ULTRA_FAST_POSITION_AMOUNT,
    choose_dynamic_delay,
    display_interval_range,
    market_activity_label,
    timing_for_position,
    update_change_score,
)


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
    assert display_interval_range("fast_1") == (1.5, 2.2)
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

    assert timing.min_seconds == 0.8
    assert timing.max_seconds == 1.3
    assert decision.delay_seconds <= 1.3
    assert display_interval_range("fast_1", position_amount=500) == (0.8, 1.3)


def test_market_activity_labels_are_human_readable() -> None:
    assert market_activity_label(0.9) == "высокая"
    assert market_activity_label(0.5) == "средняя"
    assert market_activity_label(0.1) == "низкая"
