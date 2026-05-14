import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from redis.asyncio import Redis

from app.bot.handlers.positions import router as positions_router
from app.bot.handlers.settings import router as settings_router
from app.bot.handlers.start import router as start_router
from app.bot.middlewares import OwnerOnlyMiddleware
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.repositories import AppSettingsRepository, PositionRepository
from app.db.session import create_session_factory

logger = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start bot")
    if not settings.allowed_owner_telegram_ids:
        raise RuntimeError("OWNER_TELEGRAM_IDS or OWNER_TELEGRAM_ID is required to start bot")

    session_factory = create_session_factory(settings=settings)
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

    dispatcher.include_router(start_router)
    dispatcher.include_router(positions_router)
    dispatcher.include_router(settings_router)

    logger.info("repricer_bot_started")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await redis.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
