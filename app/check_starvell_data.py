import asyncio

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.market.client import (
    KNOWN_STARVELL_LOT_IDS,
    StarvellClient,
    explain_http_status,
    parse_starvell_lots_from_html,
)
from app.market.exceptions import StarvellEndpointNotConfiguredError
from app.market.schemas import MyLotSummary
from app.repricer.rate_limiter import InMemoryFixedWindowRateLimiter


DIAGNOSTIC_FALLBACK_PATHS = (
    "/profile",
    "/account",
    "/cabinet",
    "/dashboard",
    "/seller",
)


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    print("Диагностика данных Starvell")
    print("Режим: только безопасные GET-запросы.")
    print("POST/PATCH/PUT/DELETE не выполняются.")
    print()

    limiter = InMemoryFixedWindowRateLimiter(limit=settings.request_limit_per_minute)
    async with StarvellClient(settings, limiter) as client:
        lots, checked_urls = await _load_lots(client, settings)

    report = build_starvell_data_report(lots, checked_urls=checked_urls)
    print(report)
    return 0 if _known_lots(lots) else 1


async def _load_lots(
    client: StarvellClient,
    settings: Settings,
) -> tuple[list[MyLotSummary], list[str]]:
    checked_urls: list[str] = []
    if settings.market_my_lots_url:
        checked_urls.append(settings.market_my_lots_url)
        try:
            return await client.get_my_lots(), checked_urls
        except httpx.HTTPStatusError as exc:
            print(explain_http_status(exc.response.status_code))
            return [], checked_urls
        except StarvellEndpointNotConfiguredError as exc:
            print(str(exc))
            return [], checked_urls
        except Exception as exc:
            print(f"Не удалось прочитать данные Starvell: {type(exc).__name__}")
            return [], checked_urls

    lots: list[MyLotSummary] = []
    for url in _diagnostic_candidate_urls(settings):
        checked_urls.append(url)
        try:
            text, _, _ = await client.fetch_text(url, request_type="starvell_data_diagnostic")
        except httpx.HTTPStatusError as exc:
            print(f"{url}: {explain_http_status(exc.response.status_code)}")
            continue
        except Exception:
            continue

        lots.extend(
            parse_starvell_lots_from_html(
                text,
                default_seller_id=settings.own_seller_id,
            )
        )
        if _known_lots(lots):
            break

    return _deduplicate_lots(lots), checked_urls


def build_starvell_data_report(
    lots: list[MyLotSummary],
    *,
    checked_urls: list[str],
) -> str:
    lines: list[str] = []
    if checked_urls:
        lines.append("Проверенные GET URL:")
        for url in checked_urls:
            lines.append(f"- {url}")
        lines.append("")

    lines.append(f"Всего распознано лотов: {len(lots)}")
    known_lots = _known_lots(lots)
    lines.append(f"Известных lot_id найдено: {len(known_lots)}")
    lines.append("")

    if known_lots:
        lines.append("Найденные лоты:")
        for lot in known_lots:
            lines.append(format_lot_summary(lot))
        return "\n".join(lines)

    lines.append("Известные lot_id не найдены.")
    lines.append("")
    lines.extend(_manual_instructions())
    return "\n".join(lines)


def format_lot_summary(lot: MyLotSummary) -> str:
    title = lot.title or "не найдено"
    price = f"{lot.price} ₽" if lot.price is not None else "не найдена"
    seller_id = lot.seller_id or "не найден"
    stock = str(lot.stock) if lot.stock is not None else "не найдено"
    amount = f"{lot.position_amount} робуксов" if lot.position_amount is not None else "не найдено"
    return (
        f"- lot_id: {lot.lot_id or 'не найден'}; "
        f"название: {title}; "
        f"позиция: {amount}; "
        f"наличие: {stock}; "
        f"цена: {price}; "
        f"seller_id: {seller_id}"
    )


def _diagnostic_candidate_urls(settings: Settings) -> list[str]:
    urls: list[str] = []
    if settings.market_account_info_url:
        urls.append(settings.market_account_info_url)
    if settings.own_seller_id:
        urls.append(f"/users/{settings.own_seller_id}")
    urls.extend(DIAGNOSTIC_FALLBACK_PATHS)
    return list(dict.fromkeys(urls))


def _known_lots(lots: list[MyLotSummary]) -> list[MyLotSummary]:
    return [lot for lot in lots if lot.lot_id in KNOWN_STARVELL_LOT_IDS]


def _deduplicate_lots(lots: list[MyLotSummary]) -> list[MyLotSummary]:
    unique: dict[str, MyLotSummary] = {}
    without_id: list[MyLotSummary] = []
    for lot in lots:
        if not lot.lot_id:
            without_id.append(lot)
            continue
        unique.setdefault(lot.lot_id, lot)
    return list(unique.values()) + without_id


def _manual_instructions() -> list[str]:
    return [
        "Что открыть вручную в браузере:",
        "- страницу профиля продавца Starvell, например https://starvell.com/users/4111;",
        "- раздел профиля/кабинета с активными предложениями;",
        "- карточку любого моего лота, например /offers/1996.",
        "",
        "Что искать в DevTools -> Network -> Fetch/XHR или в HTML страницы:",
        "- ссылки вида /offers/{lot_id};",
        (
            "- известные ID: 1996, 1998, 1999, 2000, 2002, 2003, 2004, "
            "2005, 2006, 2007, 2008, 2009, 2010, 2011, 2012;"
        ),
        "- слова lot, listing, offer, price, robux, робуксов.",
        "",
        "Когда найдете страницу с этими данными, укажите ее в .env:",
        "MARKET_MY_LOTS_URL=https://starvell.com/users/4111",
    ]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
