import asyncio
import contextlib
import random
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import ClientConnectorError
from redis.asyncio import Redis

from app.bot.handlers.positions import router as positions_router
from app.bot.handlers.settings import router as settings_router
from app.bot.handlers.start import router as start_router
from app.bot.middlewares import CallbackSafetyMiddleware, OwnerOnlyMiddleware
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.repositories import AppSettingsRepository, PositionRepository
from app.db.session import create_session_factory

logger = get_logger(__name__)
BOT_HEALTHCHECK_FILE = Path("/tmp/starvell_bot_polling_heartbeat")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.app_mode not in {"all", "bot"}:
        raise RuntimeError("APP_MODE must be bot or all to start bot")

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start bot")
    if not settings.allowed_owner_telegram_ids:
        raise RuntimeError("OWNER_TELEGRAM_IDS or OWNER_TELEGRAM_ID is required to start bot")

    session_factory = create_session_factory(settings=settings)
    if settings.app_mode == "all":
        async with session_factory() as session:
            repository = PositionRepository(session)
            await repository.seed_default_positions(
                min_price=settings.default_min_price,
                max_price=settings.default_max_price,
                step=settings.default_price_step,
                min_rating=settings.default_min_rating,
                ignore_no_rating=settings.default_ignore_no_rating,
                fallback_behavior=settings.default_fallback_behavior,
            )
            await AppSettingsRepository(session).ensure_defaults(dry_run=settings.dry_run)
            await session.commit()

    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["session_factory"] = session_factory
    dispatcher["settings"] = settings
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    dispatcher["redis"] = redis

    owner_middleware = OwnerOnlyMiddleware(settings)
    dispatcher.message.middleware(owner_middleware)
    dispatcher.callback_query.middleware(owner_middleware)
    dispatcher.callback_query.middleware(CallbackSafetyMiddleware())

    dispatcher.include_router(start_router)
    dispatcher.include_router(positions_router)
    dispatcher.include_router(settings_router)

    logger.info("repricer_bot_started")
    try:
        await _run_polling_forever(dispatcher, bot)
    finally:
        await redis.aclose()
        await bot.session.close()


async def _run_polling_forever(dispatcher: Dispatcher, bot: Bot) -> None:
    attempt = 0
    while True:
        heartbeat_task = asyncio.create_task(_polling_heartbeat(), name="telegram-polling-heartbeat")
        try:
            logger.info(
                "telegram_polling_restarted" if attempt else "telegram_polling_started",
                attempt=attempt + 1,
            )
            await dispatcher.start_polling(bot)
        except (TelegramNetworkError, ClientConnectorError, TimeoutError, OSError) as exc:
            attempt += 1
            delay = _polling_retry_delay(attempt)
            logger.warning(
                "telegram_api_unavailable",
                attempt=attempt,
                retry_after_seconds=round(delay, 2),
                error_type=type(exc).__name__,
            )
            await asyncio.sleep(delay)
        except Exception as exc:
            attempt += 1
            delay = 10.0
            logger.exception(
                "telegram_polling_failed",
                attempt=attempt,
                retry_after_seconds=delay,
                error_type=type(exc).__name__,
            )
            await asyncio.sleep(delay)
        else:
            logger.info("telegram_polling_stopped")
            return
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task


async def _polling_heartbeat() -> None:
    while True:
        BOT_HEALTHCHECK_FILE.write_text(str(time.time()))
        await asyncio.sleep(15)


def _polling_retry_delay(attempt: int) -> float:
    base = min(5 + max(attempt - 1, 0) * 2, 15)
    return random.uniform(5, base)


if __name__ == "__main__":
    asyncio.run(main())
