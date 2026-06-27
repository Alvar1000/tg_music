"""Команда /start и проверка подписки (гейт)."""
import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from database import db
from handlers.menu import show_main_menu
from middlewares.subscription import is_subscribed, show_gate

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.full_name)

    subscribed = await is_subscribed(message.bot, user.id)
    await db.set_subscribed(user.id, subscribed)

    if subscribed:
        await show_main_menu(message, greeting=True)
    else:
        await show_gate(message)


@router.callback_query(F.data == "check_sub")
async def check_subscription(callback: CallbackQuery) -> None:
    user = callback.from_user
    subscribed = await is_subscribed(callback.bot, user.id)
    await db.set_subscribed(user.id, subscribed)

    if subscribed:
        await callback.answer("Готово! Добро пожаловать 🤘")
        await show_main_menu(callback, greeting=True)
    else:
        # show_gate сам покажет всплывающий алерт «ещё не подписан».
        await show_gate(callback)
