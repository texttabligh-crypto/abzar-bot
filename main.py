import asyncio
from aiogram import Bot, Dispatcher

from app.core.config import settings
from app.db.init_db import init_db
from app.bot.setup import register_routers, seed_defaults, setup_bot_ui


async def main():
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")

    await init_db()
    await seed_defaults()

    bot = Bot(settings.telegram_bot_token)
    await setup_bot_ui(bot)

    dp = Dispatcher()
    register_routers(dp)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
