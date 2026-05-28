import pytest

from app.core.config import Settings


def test_owner_telegram_ids_are_parsed_from_comma_list() -> None:
    settings = Settings(
        _env_file=None,
        owner_telegram_ids="123456789, 987654321",
        owner_telegram_id=111,
    )

    assert settings.allowed_owner_telegram_ids == {123456789, 987654321}


def test_owner_telegram_id_fallback_is_kept() -> None:
    settings = Settings(_env_file=None, owner_telegram_ids="", owner_telegram_id=123456789)

    assert settings.allowed_owner_telegram_ids == {123456789}


def test_invalid_owner_telegram_ids_are_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, owner_telegram_ids="123,not-a-number")


def test_priority_percentages_must_sum_to_100() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, high_priority_percent=70, normal_priority_percent=20)


def test_worker_groups_use_two_fast_servers_and_one_slow_server(monkeypatch) -> None:
    for env_name in (
        "PROXY_FAST_1_POSITIONS",
        "PROXY_FAST_2_POSITIONS",
        "PROXY_SLOW_POSITIONS",
        "WORKER_FAST_1_POSITIONS",
        "WORKER_FAST_2_POSITIONS",
        "WORKER_SLOW_POSITIONS",
    ):
        monkeypatch.delenv(env_name, raising=False)
    settings = Settings(_env_file=None)

    groups = {info.name: info for info in settings.worker_group_infos}

    assert groups["fast_1"].positions == (500,)
    assert groups["fast_2"].positions == (400, 800, 1000, 1200, 1700, 2000)
    assert groups["slow"].positions == (40, 80, 200, 2100, 2500, 3600, 4500, 10000, 22500)


def test_legacy_worker_group_aliases_are_kept() -> None:
    fast = Settings(_env_file=None, worker_group="fast")
    medium = Settings(_env_file=None, worker_group="medium")

    assert fast.worker_group == "fast_1"
    assert medium.worker_group == "fast_2"


def test_worker_positions_can_be_overridden_from_env_values() -> None:
    settings = Settings(
        _env_file=None,
        proxy_fast_1_positions="500, 800, 1000",
        worker_group="fast_1",
    )

    assert settings.assigned_positions == (500, 800, 1000)


def test_proxy_url_for_group_is_disabled_when_proxy_mode_is_disabled() -> None:
    settings = Settings(
        _env_file=None,
        proxy_mode="disabled",
        proxy_fast_1_url="http://login:password@1.1.1.1:8000",
        worker_group="fast_1",
    )

    assert settings.proxy_url_for_group() is None


def test_proxy_limits_must_not_exceed_global_limit() -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            global_request_limit_per_minute=100,
            proxy_fast_1_request_limit_per_minute=100,
            proxy_fast_2_request_limit_per_minute=100,
            proxy_slow_request_limit_per_minute=100,
        )


def test_proxy_positions_must_not_overlap() -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            proxy_fast_1_positions="500,800",
            proxy_fast_2_positions="800,1200",
        )


def test_account_effective_limit_defaults_to_full_proxy_capacity() -> None:
    settings = Settings(_env_file=None)

    assert settings.token_limit_mode is True
    assert settings.rate_limiter_soft_cap_enabled is True
    assert settings.global_request_limit_per_minute == 240
    assert settings.account_effective_limit_per_minute == 240
    assert settings.account_min_limit_per_minute == 60
    assert settings.account_limit_ramp_step_per_minute == 30
    assert settings.account_limit_ramp_idle_seconds == 60.0
    assert settings.price_update_context_cache_ttl_seconds == 300.0
    assert settings.proxy_request_limits["fast_1"] == 100
    assert settings.proxy_request_limits["fast_2"] == 90
    assert settings.proxy_request_limits["slow"] == 50


def test_request_pacing_defaults_are_safe_without_group_overrides() -> None:
    settings = Settings(_env_file=None)

    assert settings.request_min_delay_ms == 300
    assert settings.request_jitter_ms == 200
    assert settings.request_delay_for_group("fast_1") == (200, 100)
    assert settings.request_delay_for_group("fast_2") == (300, 200)
    assert settings.scheduler_max_concurrent_positions == 2
    assert settings.scheduler_idle_sleep_for_group("fast_1") == 0.1
    assert settings.scheduler_idle_sleep_for_group("fast_2") == 0.1
    assert settings.scheduler_idle_sleep_for_group("slow") == 1.0
    assert settings.ultra_fast_min_interval_seconds == 0.4
    assert settings.fast1_min_interval_seconds == 0.8
    assert settings.fast2_min_interval_seconds == 2.0
    assert settings.hot_mode_enabled is False
    assert settings.hot_mode_min_interval_seconds == 0.25
    assert settings.hot_mode_max_interval_seconds == 0.5
    assert settings.hot_mode_cooldown_interval_seconds == 1.0
    assert settings.hot_mode_skipped_threshold == 3
    assert settings.hot_mode_window_seconds == 10.0
    assert settings.fast_mode_enabled is False
    assert settings.fast_mode_min_interval_seconds == 0.8
    assert settings.fast_mode_max_interval_seconds == 1.2
    assert settings.fast_mode_cooldown_min_interval_seconds == 1.5
    assert settings.fast_mode_cooldown_max_interval_seconds == 2.5
    assert settings.post_update_interval_multiplier == 1.0
    assert settings.post_update_interval_position_amounts == (500,)


def test_request_pacing_can_be_overridden_per_worker_group() -> None:
    settings = Settings(
        _env_file=None,
        request_min_delay_ms=100,
        request_jitter_ms=50,
        fast1_min_delay_ms=25,
        fast1_jitter_ms=10,
        fast2_min_delay_ms=75,
        slow_min_delay_ms=250,
    )

    assert settings.request_delay_for_group("fast_1") == (25, 10)
    assert settings.request_delay_for_group("fast_2") == (75, 50)
    assert settings.request_delay_for_group("slow") == (250, 50)


def test_market_http_transport_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.market_http2_enabled is False
    assert settings.market_http_timeout_seconds == 15.0
    assert settings.market_http_max_connections == 20
    assert settings.market_http_max_keepalive_connections == 10
    assert settings.my_lot_state_cache_ttl_seconds == 30.0


def test_price_write_settings_default_to_safe_analysis_mode() -> None:
    settings = Settings(
        _env_file=None,
        enable_real_price_writes=False,
        market_update_lot_price_url="",
        market_update_lot_price_method="POST",
        market_update_price_payload_style="partial_update",
        market_update_price_content_type="json",
    )

    assert settings.enable_real_price_writes is False
    assert settings.market_update_lot_price_url == ""
    assert settings.market_update_lot_price_method == "POST"
    assert settings.market_update_price_payload_style == "partial_update"
    assert settings.market_update_price_content_type == "json"


def test_price_write_method_is_validated() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, market_update_lot_price_method="GET")


def test_price_write_payload_style_is_validated() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, market_update_price_payload_style="unknown")


def test_price_write_content_type_is_validated() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, market_update_price_content_type="xml")
