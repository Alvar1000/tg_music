"""Точка входа: логирование, бот, диспетчер и запуск long polling."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from database import db
from handlers import admin, events, facts, fallback, menu, start, tests
from middlewares.subscription import SubscriptionMiddleware

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Логи уровня INFO в консоль и в файл."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        ],
    )


def create_bot() -> Bot:
    return Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Гейт подписки — на каждое сообщение и нажатие кнопки.
    gate = SubscriptionMiddleware()
    dp.message.outer_middleware(gate)
    dp.callback_query.outer_middleware(gate)

    # start (/start и check_sub) включаем первым.
    dp.include_router(start.router)
    dp.include_router(menu.router)
    dp.include_router(facts.router)
    dp.include_router(tests.router)
    dp.include_router(events.router)
    dp.include_router(admin.router)
    # Фолбэк для устаревших кнопок — строго последним, чтобы не перехватывать
    # настоящие callback'и (роутеры проверяются по порядку включения).
    dp.include_router(fallback.router)
    return dp


async def main() -> None:
    setup_logging()
    config.validate()  # упадём с понятной ошибкой, если .env не заполнен
    config.seed_playlists()  # на хостинге с диском — перенесём стартовую очередь

    await db.init_db()
    bot = create_bot()
    dp = create_dispatcher()

    try:
        logger.info("Бот запускается (long polling)...")
        # drop_pending_updates: при старте отбрасываем накопившиеся за простой
        # апдейты, чтобы не отвечать на устаревшие нажатия («query is too old»).
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()
        await db.close_db()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Выход по сигналу.")
