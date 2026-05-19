from app.core.config import Settings
from app.repricer.scheduler import RepricerScheduler


def test_scheduler_classifies_proxy_transport_errors() -> None:
    scheduler = RepricerScheduler.__new__(RepricerScheduler)

    assert scheduler._error_kind("failed", "proxy_malformed_reply") == "proxy"
    assert scheduler._error_kind("failed", "socksio ProtocolError: Malformed reply") == "proxy"


def test_scheduler_enters_safe_mode_for_proxy_errors() -> None:
    scheduler = RepricerScheduler.__new__(RepricerScheduler)
    scheduler.settings = Settings(_env_file=None, safe_mode_enabled=True)
    scheduler.consecutive_errors = 1

    assert scheduler._should_enter_safe_mode("proxy") is True


def test_scheduler_uses_short_safe_mode_for_proxy_transport_errors() -> None:
    scheduler = RepricerScheduler.__new__(RepricerScheduler)
    scheduler.consecutive_errors = 1

    assert scheduler._safe_mode_delay_seconds("proxy") == 0.2

    scheduler.consecutive_errors = 2

    assert scheduler._safe_mode_delay_seconds("proxy") == 0.5
