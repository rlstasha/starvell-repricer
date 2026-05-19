import app.bot_main as bot_main


def test_polling_retry_delay_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(bot_main.random, "uniform", lambda low, high: high)

    assert bot_main._polling_retry_delay(1) == 5
    assert bot_main._polling_retry_delay(4) == 11
    assert bot_main._polling_retry_delay(99) == 15
