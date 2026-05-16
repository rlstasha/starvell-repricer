import argparse
import asyncio
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
        )
    )


async def _run(
    *,
    settings: Settings,
    lot_id: str,
    position_amount: int,
    price: Decimal,
    confirm: bool,
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

    if not confirm:
        print()
        print("Реальный запрос НЕ отправлен.")
        print("Для отправки добавьте --confirm.")
        return 0

    limiter = InMemoryFixedWindowRateLimiter(limit=settings.worker_request_limit_per_minute)
    async with StarvellClient(
        settings,
        limiter,
        proxy_profile=group,
        proxy_url=proxy_url,
    ) as client:
        try:
            result = await client.update_my_lot_price(
                position_amount=position_amount,
                lot_id=lot_id,
                new_price=price,
                allow_real_write=True,
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
