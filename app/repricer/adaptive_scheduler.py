import os
import random
from dataclasses import dataclass
from decimal import Decimal

from app.repricer.worker_groups import (
    WORKER_GROUP_FAST_1,
    WORKER_GROUP_FAST_2,
    WORKER_GROUP_SLOW,
)


@dataclass(frozen=True)
class ProfileTiming:
    base_seconds: float
    min_seconds: float
    max_seconds: float
    normal_min_seconds: float
    normal_max_seconds: float
    backoff_min_seconds: float
    backoff_max_seconds: float


@dataclass(frozen=True)
class DelayDecision:
    delay_seconds: float
    reason: str
    range_min_seconds: float
    range_max_seconds: float


ACTIVE_STRATEGY_REASONS = frozenset(
    {
        "min_price_bounce_to_upper_competitor",
        "all_competitors_below_min_price",
        "competitor_above_min_but_step_hits_min",
    }
)
ACTIVE_STRATEGY_CHANGE_SCORE_FLOOR = 0.65
_interval_override_cache: dict[str, float | None] = {}

PROFILE_TIMINGS = {
    WORKER_GROUP_FAST_1: ProfileTiming(
        base_seconds=1.8,
        min_seconds=1.5,
        max_seconds=2.7,
        normal_min_seconds=1.5,
        normal_max_seconds=2.2,
        backoff_min_seconds=2.5,
        backoff_max_seconds=4.0,
    ),
    WORKER_GROUP_FAST_2: ProfileTiming(
        base_seconds=2.4,
        min_seconds=2.0,
        max_seconds=3.0,
        normal_min_seconds=2.0,
        normal_max_seconds=3.0,
        backoff_min_seconds=3.0,
        backoff_max_seconds=5.0,
    ),
    WORKER_GROUP_SLOW: ProfileTiming(
        base_seconds=5.4,
        min_seconds=4.5,
        max_seconds=9.0,
        normal_min_seconds=4.5,
        normal_max_seconds=6.5,
        backoff_min_seconds=6.0,
        backoff_max_seconds=8.0,
    ),
}
ULTRA_FAST_POSITION_AMOUNT = 500
ULTRA_FAST_TIMING = ProfileTiming(
    base_seconds=1.0,
    min_seconds=0.8,
    max_seconds=1.3,
    normal_min_seconds=0.8,
    normal_max_seconds=1.3,
    backoff_min_seconds=2.0,
    backoff_max_seconds=4.0,
)
DEFAULT_TIMING = ProfileTiming(
    base_seconds=6.0,
    min_seconds=3.0,
    max_seconds=12.0,
    normal_min_seconds=3.0,
    normal_max_seconds=8.0,
    backoff_min_seconds=8.0,
    backoff_max_seconds=12.0,
)


def timing_for_group(worker_group: str) -> ProfileTiming:
    timing = PROFILE_TIMINGS.get(worker_group, DEFAULT_TIMING)
    if worker_group == WORKER_GROUP_FAST_1:
        return _with_min_interval_override(timing, "FAST1_MIN_INTERVAL_SECONDS")
    if worker_group == WORKER_GROUP_FAST_2:
        return _with_min_interval_override(timing, "FAST2_MIN_INTERVAL_SECONDS")
    return timing


def timing_for_position(worker_group: str, position_amount: int | None) -> ProfileTiming:
    if worker_group == WORKER_GROUP_FAST_1 and position_amount == ULTRA_FAST_POSITION_AMOUNT:
        return _with_min_interval_override(ULTRA_FAST_TIMING, "ULTRA_FAST_MIN_INTERVAL_SECONDS")
    return timing_for_group(worker_group)


def update_change_score(
    previous_score: float,
    previous_competitor_price: Decimal | None,
    current_competitor_price: Decimal | None,
) -> float:
    observed_change = 0.0
    if previous_competitor_price is not None and current_competitor_price is not None:
        observed_change = 1.0 if previous_competitor_price != current_competitor_price else 0.0
    return _clamp(previous_score * 0.7 + observed_change * 0.3, 0.0, 1.0)


def update_error_score(previous_score: float, *, failed: bool) -> float:
    observed_error = 1.0 if failed else 0.0
    return _clamp(previous_score * 0.7 + observed_error * 0.3, 0.0, 1.0)


def is_active_strategy_reason(reason: str | None) -> bool:
    if reason is None:
        return False
    normalized_reason = reason.split(":", 1)[-1]
    return normalized_reason in ACTIVE_STRATEGY_REASONS


def apply_strategy_activity_floor(change_score: float, reason: str | None) -> float:
    if not is_active_strategy_reason(reason):
        return change_score
    return max(change_score, ACTIVE_STRATEGY_CHANGE_SCORE_FLOOR)


def choose_dynamic_delay(
    *,
    worker_group: str,
    position_amount: int | None = None,
    change_score: float,
    error_score: float = 0.0,
    backoff_active: bool = False,
    previous_delay_seconds: float | None = None,
    random_uniform=random.uniform,
) -> DelayDecision:
    timing = timing_for_position(worker_group, position_amount)

    if backoff_active:
        delay = random_uniform(timing.backoff_min_seconds, timing.backoff_max_seconds)
        delay = _avoid_repeated_delay(
            delay,
            previous_delay_seconds,
            timing.backoff_min_seconds,
            timing.backoff_max_seconds,
        )
        return DelayDecision(
            delay_seconds=round(delay, 2),
            reason="backoff_after_429",
            range_min_seconds=timing.backoff_min_seconds,
            range_max_seconds=timing.backoff_max_seconds,
        )

    interval = timing.base_seconds
    reason = "normal"
    if change_score > 0.8:
        interval *= 0.75
        reason = "market_hot"
    elif change_score < 0.3:
        interval *= 1.4
        reason = "market_calm"
    if error_score > 0.5:
        interval *= 1.2
        reason = "recent_errors"

    jitter = random_uniform(0.15, 0.25)
    low = max(timing.min_seconds, interval * (1 - jitter))
    high = min(timing.max_seconds, interval * (1 + jitter))
    if high < low:
        high = low
    delay = random_uniform(low, high)
    delay = _avoid_repeated_delay(delay, previous_delay_seconds, timing.min_seconds, timing.max_seconds)
    return DelayDecision(
        delay_seconds=round(delay, 2),
        reason=reason,
        range_min_seconds=round(low, 2),
        range_max_seconds=round(high, 2),
    )


def display_interval_range(
    worker_group: str,
    *,
    position_amount: int | None = None,
    backoff_active: bool = False,
) -> tuple[float, float]:
    timing = timing_for_position(worker_group, position_amount)
    if backoff_active:
        return timing.backoff_min_seconds, timing.backoff_max_seconds
    return timing.normal_min_seconds, timing.normal_max_seconds


def market_activity_label(change_score: float | None) -> str:
    if change_score is None:
        return "нет данных"
    if change_score >= 0.65:
        return "высокая"
    if change_score >= 0.3:
        return "средняя"
    return "низкая"


def _avoid_repeated_delay(
    delay: float,
    previous_delay: float | None,
    min_seconds: float,
    max_seconds: float,
) -> float:
    if previous_delay is None or abs(delay - previous_delay) >= 0.05:
        return delay
    if delay + 0.12 <= max_seconds:
        return delay + 0.12
    if delay - 0.12 >= min_seconds:
        return delay - 0.12
    return min(max(delay + 0.07, min_seconds), max_seconds)


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def _with_min_interval_override(timing: ProfileTiming, env_name: str) -> ProfileTiming:
    min_seconds = _interval_override_value(env_name)
    if min_seconds is None:
        return timing
    min_seconds = max(min_seconds, 0.01)
    return ProfileTiming(
        base_seconds=timing.base_seconds,
        min_seconds=min_seconds,
        max_seconds=max(timing.max_seconds, min_seconds),
        normal_min_seconds=min_seconds,
        normal_max_seconds=max(timing.normal_max_seconds, min_seconds),
        backoff_min_seconds=timing.backoff_min_seconds,
        backoff_max_seconds=timing.backoff_max_seconds,
    )


def _interval_override_value(env_name: str) -> float | None:
    if env_name not in _interval_override_cache:
        _interval_override_cache[env_name] = _read_interval_override_value(env_name)
    return _interval_override_cache[env_name]


def _clear_interval_override_cache() -> None:
    _interval_override_cache.clear()


def _read_interval_override_value(env_name: str) -> float | None:
    raw_value = os.getenv(env_name)
    if raw_value is not None and raw_value.strip() != "":
        try:
            return float(raw_value)
        except ValueError:
            return None
    settings_field = {
        "ULTRA_FAST_MIN_INTERVAL_SECONDS": "ultra_fast_min_interval_seconds",
        "FAST1_MIN_INTERVAL_SECONDS": "fast1_min_interval_seconds",
        "FAST2_MIN_INTERVAL_SECONDS": "fast2_min_interval_seconds",
    }.get(env_name)
    if settings_field is None:
        return None
    try:
        from app.core.config import get_settings

        return float(getattr(get_settings(), settings_field))
    except Exception:
        return None
