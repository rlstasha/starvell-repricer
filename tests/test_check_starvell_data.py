from decimal import Decimal

from app.check_starvell_data import build_starvell_data_report, format_lot_summary
from app.market.schemas import MyLotSummary


def test_check_starvell_data_report_shows_known_lot_public_fields() -> None:
    report = build_starvell_data_report(
        [
            MyLotSummary(
                lot_id="1996",
                title="80 робуксов",
                position_amount=80,
                stock=927,
                price=Decimal("92.67"),
                seller_id="4111",
            )
        ],
        checked_urls=["https://starvell.example/users/4111"],
    )

    assert "lot_id: 1996" in report
    assert "название: 80 робуксов" in report
    assert "наличие: 927" in report
    assert "цена: 92.67 ₽" in report
    assert "seller_id: 4111" in report


def test_check_starvell_data_report_prints_manual_instructions_when_empty() -> None:
    report = build_starvell_data_report([], checked_urls=[])

    assert "Известные lot_id не найдены" in report
    assert "страницу профиля продавца Starvell" in report
    assert "DevTools -> Network" in report
    assert "MARKET_MY_LOTS_URL=https://starvell.com/users/4111" in report


def test_check_starvell_data_report_does_not_include_secret_words() -> None:
    secret_value = "super-secret-cookie"
    report = build_starvell_data_report(
        [
            MyLotSummary(
                lot_id="1998",
                title="200 робуксов",
                position_amount=200,
                price=Decimal("220"),
                seller_id="4111",
            )
        ],
        checked_urls=["https://starvell.example/users/4111"],
    )
    summary = format_lot_summary(
        MyLotSummary(
            lot_id="1998",
            title="200 робуксов",
            position_amount=200,
            price=Decimal("220"),
            seller_id="4111",
        )
    )

    assert secret_value not in report
    assert secret_value not in summary
    assert "cookie" not in report.lower()
    assert "session" not in report.lower()
    assert "token" not in report.lower()
    assert "csrf" not in report.lower()
