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


def test_worker_groups_use_two_fast_servers_and_one_slow_server() -> None:
    settings = Settings(_env_file=None)

    groups = {info.name: info for info in settings.worker_group_infos}

    assert groups["fast_1"].positions == (500, 800, 1000)
    assert groups["fast_2"].positions == (400, 1200, 1700, 2000)
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
    assert settings.account_effective_limit_per_minute == 300
    assert settings.account_min_limit_per_minute == 60


def test_request_pacing_defaults_are_fast_when_proxies_are_healthy() -> None:
    settings = Settings(_env_file=None)

    assert settings.request_min_delay_ms == 100
    assert settings.request_jitter_ms == 50


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
