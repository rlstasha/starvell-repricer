import argparse
import asyncio
import json
from decimal import Decimal, InvalidOperation

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.repositories import DEFAULT_LOT_IDS
from app.market.client import StarvellClient, safe_starvell_error_reason
from app.market.exceptions import (
    StarvellEndpointNotConfiguredError,
    StarvellPayloadStyleError,
    StarvellWriteDisabledError,
)
from app.market.schemas import PriceUpdateAttemptResult
from app.repricer.rate_limiter import InMemoryFixedWindowRateLimiter


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely inspect or execute one Starvell price update request."
    )
    parser.add_argument("--lot-id", required=True, help="Starvell lot/listing ID")
    parser.add_argument("--price", required=True, help="New price in rubles")
    parser.add_argument(
        "--position-amount",
        type=int,
        default=None,
        help="Robux amount; inferred for known lot IDs when omitted",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually send the configured write request",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Send diagnostic write attempts with known payload/content-type variants "
            "and print sanitized Starvell responses"
        ),
    )
    args = parser.parse_args()

    try:
        price = Decimal(args.price)
    except (InvalidOperation, ValueError):
        print("Ошибка: --price должен быть числом.")
        return 2

    settings = get_settings()
    configure_logging(settings.log_level)
    position_amount = args.position_amount or _position_amount_for_lot(args.lot_id) or 0
    return asyncio.run(
        _run(
            settings=settings,
            lot_id=args.lot_id,
            position_amount=position_amount,
            price=price,
            confirm=args.confirm,
            debug=args.debug,
        )
    )


async def _run(
    *,
    settings: Settings,
    lot_id: str,
    position_amount: int,
    price: Decimal,
    confirm: bool,
    debug: bool,
) -> int:
    group = _group_for_position(settings, position_amount)
    proxy_url = settings.proxy_url_for_group(group) if group else None

    print("Тест изменения цены Starvell")
    print(f"lot_id: {lot_id}")
    print(f"position_amount: {position_amount or 'не указан'}")
    print(f"price: {price}")
    print(f"profile: {group or 'direct'}")
    print(f"endpoint: {'configured' if settings.market_update_lot_price_url else 'missing'}")
    print(f"method: {settings.market_update_lot_price_method}")
    print(f"payload style: {settings.market_update_price_payload_style}")
    print(f"content type: {settings.market_update_price_content_type}")

    if not confirm:
        print()
        print("Реальный запрос НЕ отправлен.")
        if debug:
            print("Debug-режим подготовлен, но diagnostic POST/PATCH/PUT тоже не отправлен.")
            print("Чтобы получить response body/headers Starvell, добавьте --confirm.")
        else:
            print("Для отправки добавьте --confirm.")
        return 0

    if debug:
        print()
        print("Debug mode: будут отправлены реальные POST/PATCH/PUT попытки, если запись включена.")
        print("Секреты в выводе маскируются.")

    limiter = InMemoryFixedWindowRateLimiter(limit=settings.worker_request_limit_per_minute)
    async with StarvellClient(
        settings,
        limiter,
        proxy_profile=group,
        proxy_url=proxy_url,
    ) as client:
        try:
            if debug:
                attempts = await client.debug_my_lot_price_update(
                    position_amount=position_amount,
                    lot_id=lot_id,
                    new_price=price,
                    allow_real_write=not settings.dry_run,
                )
                _print_debug_attempts(attempts)
                return 0 if any(attempt.success for attempt in attempts) else 1

            result = await client.update_my_lot_price(
                position_amount=position_amount,
                lot_id=lot_id,
                new_price=price,
                allow_real_write=not settings.dry_run,
            )
        except (
            StarvellEndpointNotConfiguredError,
            StarvellPayloadStyleError,
            StarvellWriteDisabledError,
        ) as exc:
            print()
            print("Реальный запрос не выполнен.")
            print(f"Причина: {exc}")
            return 1
        except Exception as exc:
            print()
            print("Реальный запрос завершился ошибкой.")
            print(f"Причина: {safe_starvell_error_reason(exc)}")
            return 1

    print()
    print(f"status: {'success' if result.success else 'failed'}")
    return 0 if result.success else 1


def _print_debug_attempts(attempts: list[PriceUpdateAttemptResult]) -> None:
    if not attempts:
        print()
        print("Диагностические попытки не выполнены.")
        return

    print()
    print("Диагностика ответа Starvell")
    for index, attempt in enumerate(attempts, start=1):
        print()
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"attempt: {index}")
        print("request:")
        print(f"{attempt.method} {attempt.url}")
        print(f"content-type: {attempt.request_content_type}")
        print("payload:")
        print(json.dumps(attempt.payload, ensure_ascii=False, indent=2))
        print()
        print("response:")
        print(f"status={attempt.status_code if attempt.status_code is not None else 'no_response'}")
        if attempt.reason:
            print(f"reason={attempt.reason}")
        print(f"content-type: {attempt.response_content_type or ''}")
        print("headers:")
        print(json.dumps(attempt.response_headers, ensure_ascii=False, indent=2))
        print("body:")
        print(attempt.response_body or "")
        if attempt.success:
            print()
            print("Успех: Starvell принял этот вариант. Остальные варианты не отправлялись.")
            break


def _position_amount_for_lot(lot_id: str) -> int | None:
    for amount, known_lot_id in DEFAULT_LOT_IDS.items():
        if str(known_lot_id) == str(lot_id):
            return amount
    return None


def _group_for_position(settings: Settings, amount: int) -> str | None:
    if amount <= 0:
        return None
    for info in settings.worker_group_infos:
        if amount in info.positions:
            return info.name
    return None


if __name__ == "__main__":
    raise SystemExit(main())
