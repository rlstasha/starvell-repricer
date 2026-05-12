import asyncio
import os


async def main() -> None:
    component = os.getenv("APP_COMPONENT", "worker")
    if component == "bot":
        from app.bot_main import main as run_bot

        await run_bot()
        return
    if component == "worker":
        from app.worker_main import main as run_worker

        await run_worker()
        return
    raise RuntimeError("APP_COMPONENT must be 'worker' or 'bot'")


if __name__ == "__main__":
    asyncio.run(main())

