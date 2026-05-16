from decimal import Decimal

from app.repricer.adaptive_scheduler import (
    choose_dynamic_delay,
    display_interval_range,
    market_activity_label,
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


def test_market_activity_labels_are_human_readable() -> None:
    assert market_activity_label(0.9) == "высокая"
    assert market_activity_label(0.5) == "средняя"
    assert market_activity_label(0.1) == "низкая"
