import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.market.client import StarvellClient
from app.market.schemas import StarvellConnectionCheck
from app.repricer.rate_limiter import InMemoryFixedWindowRateLimiter


DEVTOOLS_HINTS = [
    (
        "MARKET_ACCOUNT_INFO_URL: GET-запрос кабинета/профиля, "
        "который возвращает текущего пользователя."
    ),
    (
        "MARKET_MY_LOTS_URL: GET-запрос кабинета, "
        "который возвращает мои активные лоты."
    ),
]


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    limiter = InMemoryFixedWindowRateLimiter(limit=settings.request_limit_per_minute)
    async with StarvellClient(settings, limiter) as client:
        result = await client.check_connection()

    _print_result(result)
    return 1 if _has_real_connection_error(result) else 0


def _print_result(result: StarvellConnectionCheck) -> None:
    print("Проверка подключения Starvell")
    print("Режим: только безопасные GET-запросы.")
    print("POST/PATCH/PUT не выполняются.")
    print()

    print("Авторизация:")
    if result.authorized is True:
        print("- подтверждена безопасным GET-запросом")
    elif result.authorized is False:
        print("- не подтверждена")
    else:
        print("- не проверена")
        print("  Не настроен или не найден GET endpoint аккаунта.")

    print()
    print("Аккаунт:")
    if result.account_info:
        print(f"- seller_id: {result.account_info.seller_id or 'не найден'}")
        print(f"- username: {result.account_info.seller_username or 'не найден'}")
    elif result.account_error:
        print(f"- {result.account_error}")

    print()
    print("Мои лоты:")
    if result.my_lots:
        active_lots = [lot for lot in result.my_lots if lot.is_active is not False]
        print(f"- найдено лотов: {len(result.my_lots)}")
        print(
            "- активных или без явного статуса inactive: "
            f"{len(active_lots)}"
        )
        for lot in active_lots[:10]:
            parts = [f"ID {lot.lot_id or 'не найден'}"]
            if lot.position_amount:
                parts.append(f"{lot.position_amount} робуксов")
            if lot.title:
                parts.append(lot.title)
            if lot.stock is not None:
                parts.append(f"наличие {lot.stock}")
            if lot.price is not None:
                parts.append(f"цена {lot.price} ₽")
            if lot.seller_id:
                parts.append(f"seller_id {lot.seller_id}")
            print(f"  - {' · '.join(parts)}")
    elif result.lots_error:
        print(f"- {result.lots_error}")
    else:
        print("- лоты не найдены в ответе")
        print(
            "  Проверьте MARKET_SESSION_COOKIE и что MARKET_MY_LOTS_URL "
            "открывает профиль с активными лотами."
        )

    if not result.account_endpoint_configured or not result.lots_endpoint_configured:
        print()
        print("GET endpoints Starvell пока неизвестны.")
        print("Я их не угадываю и не делаю небезопасные запросы.")
        print("Нужно найти через DevTools -> Network и добавить в .env:")
        for item in DEVTOOLS_HINTS:
            print(f"- {item}")
        print()
        print("Cookie/token не выводятся.")
        print("MARKET_CSRF_TOKEN может оставаться пустым для GET.")


def _has_real_connection_error(result: StarvellConnectionCheck) -> bool:
    status_codes = [result.account_status_code, result.lots_status_code]
    if any(status in {401, 403, 429} for status in status_codes):
        return True
    if any(status is not None and status >= 500 for status in status_codes):
        return True

    configured_errors = [
        result.account_endpoint_configured and result.account_error,
        result.lots_endpoint_configured and result.lots_error,
    ]
    return any(configured_errors)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
