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
