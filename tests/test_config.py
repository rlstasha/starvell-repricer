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
        worker_fast_1_positions="500, 800, 1000",
        worker_group="fast_1",
    )

    assert settings.assigned_positions == (500, 800, 1000)
