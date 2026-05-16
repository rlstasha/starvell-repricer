from app.core.config import get_settings


DEVTOOLS_INSTRUCTIONS = [
    "Откройте Starvell в браузере и войдите в аккаунт.",
    "Откройте свой лот, цену которого можно безопасно поменять вручную.",
    "Откройте DevTools -> Network.",
    "Измените цену вручную и нажмите сохранение на сайте.",
    "Найдите POST/PATCH/PUT запрос, который ушел при сохранении.",
    "Скопируйте URL, Method, payload и content-type без cookie/session/csrf/token.",
    "Добавьте URL в MARKET_UPDATE_LOT_PRICE_URL и выберите payload style/content type.",
]

DISCOVERED_STARVELL_CANDIDATES = [
    {
        "url": "https://starvell.com/api/offers/{lot_id}/partial-update",
        "method": "POST",
        "payload": (
            '{"availability":927,"price":"123",'
            '"minOrderCurrencyAmount":null,"isActive":true}'
        ),
        "note": (
            "найден в frontend-коде страницы списка предложений; "
            "она вызывает partialUpdate для каждой измененной строки"
        ),
    },
    {
        "url": "https://starvell.com/api/offers/{lot_id}/update",
        "method": "POST",
        "payload": '{"price": 123}',
        "note": "обычная форма редактирования лота; Starvell просит не использовать ее для массового обновления",
    },
    {
        "url": "https://starvell.com/api/offers/list-my",
        "method": "POST",
        "payload": '{"categoryId":333,"limit":20,"offset":0}',
        "note": "endpoint списка моих предложений, откуда frontend берет строки для partialUpdate",
    },
]


def main() -> int:
    settings = get_settings()
    endpoint_configured = bool(settings.market_update_lot_price_url)
    can_update = (
        not settings.dry_run
        and settings.enable_real_price_writes
        and endpoint_configured
    )

    print("Проверка настройки изменения цен Starvell")
    print()
    print(f"Price mode: {'real changes' if not settings.dry_run else 'analysis only'}")
    print(f"Real writes: {'enabled' if settings.enable_real_price_writes else 'disabled'}")
    print(f"Endpoint: {'configured' if endpoint_configured else 'missing'}")
    print(f"Method: {settings.market_update_lot_price_method}")
    print(f"Payload style: {settings.market_update_price_payload_style}")
    print(f"Content type: {settings.market_update_price_content_type}")
    print(f"Proxy mode: {settings.proxy_mode}")
    print(f"Can update prices: {'yes' if can_update else 'no'}")

    if not can_update:
        print()
        print("Почему реальные изменения сейчас не выполнятся:")
        if settings.dry_run:
            print("- включен режим только анализа")
        if not settings.enable_real_price_writes:
            print("- ENABLE_REAL_PRICE_WRITES=false")
        if not endpoint_configured:
            print("- не настроен MARKET_UPDATE_LOT_PRICE_URL")

    if not endpoint_configured:
        print()
        print("Кандидаты, найденные в frontend-коде Starvell безопасными GET-запросами:")
        for item in DISCOVERED_STARVELL_CANDIDATES:
            print(f"- URL: {item['url']}")
            print(f"  Method: {item['method']}")
            print(f"  Payload: {item['payload']}")
            print(f"  Примечание: {item['note']}")
        print()
        print("Как найти endpoint изменения цены:")
        for index, item in enumerate(DEVTOOLS_INSTRUCTIONS, start=1):
            print(f"{index}. {item}")
        print()
        print("Не вставляйте в чат и не коммитьте cookie, session, csrf, token или proxy password.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
