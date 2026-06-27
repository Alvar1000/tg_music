"""Мидлварь подписки — единый «вахтёр» перед всем функционалом.

Перед каждым действием (кроме /start, кнопки «Я подписался» и админских команд)
проверяем подписку на канал. Не подписан -> показываем гейт и хендлер не вызываем.
Так канал остаётся единственной точкой входа в функционал бота.
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject

import config
from database import db
from keyboards.kb import gate_kb

logger = logging.getLogger(__name__)

# Статусы, которые считаем «подписан».
_SUBSCRIBED_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
}

GATE_TEXT = (
    "🤘 Привет! Это бот сообщества <b>Black Magic Woman</b> — про рок и музыку.\n\n"
    "Чтобы пользоваться ботом, подпишись на канал: там плейлисты, факты, тесты "
    "и анонсы. После подписки нажми «Я подписался»."
)

ALERT_NOT_SUBSCRIBED = "Ты ещё не подписан 🤘 Подпишись и нажми кнопку снова"


async def is_subscribed(bot, user_id: int) -> bool:
    """Проверяет подписку через get_chat_member. При ошибке считает «не подписан».

    Самая частая причина ошибки — бот не админ канала (см. README).
    """
    try:
        member = await bot.get_chat_member(config.CHANNEL_ID, user_id)
        return member.status in _SUBSCRIBED_STATUSES
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось проверить подписку user_id=%s: %s", user_id, e)
        return False


async def show_gate(event: Message | CallbackQuery) -> None:
    """Показывает экран-гейт.

    Для сообщения — отправляет новое; для callback — всплывающий алерт + гейт.
    """
    kb = gate_kb(config.CHANNEL_URL)
    if isinstance(event, CallbackQuery):
        await event.answer(ALERT_NOT_SUBSCRIBED, show_alert=True)
        try:
            await event.message.edit_text(GATE_TEXT, reply_markup=kb)
        except TelegramBadRequest:
            await event.message.answer(GATE_TEXT, reply_markup=kb)
    else:
        await event.answer(GATE_TEXT, reply_markup=kb)


class SubscriptionMiddleware(BaseMiddleware):
    """Висит на dp.message и dp.callback_query как outer-мидлварь."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot = data["bot"]
        # Мидлварь стоит на message/callback_query, поэтому from_user всегда есть.
        user = event.from_user
        if user is None:
            return await handler(event, data)

        # /start и check_sub сами разбираются с гейтом — пропускаем их.
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        # Админов не запираем, иначе они не смогут вызвать /stats.
        if user.id in config.ADMIN_IDS:
            return await handler(event, data)

        # Основная проверка подписки.
        subscribed = await is_subscribed(bot, user.id)
        await db.set_subscribed(user.id, subscribed)
        if subscribed:
            return await handler(event, data)

        # Не подписан — показываем гейт и дальше не пускаем.
        await show_gate(event)
        return None
