"""Ответ соперника на личное приглашение в рок-дуэль."""
import asyncio
import logging
from html import escape

from aiogram.exceptions import TelegramAPIError
from aiogram import F, Router
from aiogram.types import CallbackQuery

import config
from database import db
from duel_content import load_rounds
from keyboards.kb import duel_play_kb, gate_kb
from middlewares.subscription import member_is_subscribed
from handlers.menu import safe_edit

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.regexp(r"^duel:d:[0-9]{1,18}$"))
async def decline(callback: CallbackQuery) -> None:
    try:
        status = await db.close_duel_challenge(int(callback.data.split(":")[-1]), callback.from_user.id, "decline")
    except ValueError:
        await callback.answer("Этот вызов недоступен.")
        return
    if status == "active":
        await callback.answer("Дуэль уже началась.")
        return
    text = "Вызов отклонён." if status == "declined" else "Этот вызов уже закрыт."
    await callback.answer(text)
    if callback.message:
        await safe_edit(callback, text)


@router.callback_query(F.data.regexp(r"^duel:a:[0-9]{1,18}$"))
async def accept(callback: CallbackQuery) -> None:
    actor = callback.from_user
    try:
        member = await callback.bot.get_chat_member(config.CHANNEL_ID, actor.id)
    except (TelegramAPIError, asyncio.TimeoutError):
        await callback.answer("Не удалось проверить подписку. Попробуй ещё раз.", show_alert=True)
        return
    subscribed = member_is_subscribed(member)
    await db.set_subscribed(actor.id, subscribed, actor.username, actor.full_name)
    if not subscribed:
        await callback.answer("Для игры нужна подписка.")
        await callback.bot.send_message(actor.id, "Подпишись на канал и снова нажми «Принять» в приглашении.",
                                        reply_markup=gate_kb(config.CHANNEL_URL))
        return
    duel_id = int(callback.data.split(":")[-1])
    rounds, _ = load_rounds()
    try:
        accepted = await db.accept_duel(duel_id, actor.id, rounds)
    except ValueError as exc:
        await callback.answer("Задания временно недоступны." if str(exc) == "content_unavailable"
                              else "Этот вызов недоступен.", show_alert=True)
        return
    if accepted["status"] != "active":
        await callback.answer("Этот вызов уже закрыт.")
        return
    kb = duel_play_kb(config.WEBAPP_URL, duel_id)
    try:
        await callback.answer("Дуэль началась!")
    except (TelegramAPIError, asyncio.TimeoutError):
        logger.info("Не удалось подтвердить нажатие для дуэли %s", duel_id)
    edit = getattr(callback.message, "edit_text", None)
    edited = False
    if edit:
        try:
            await edit("Дуэль началась!", reply_markup=kb)
            edited = True
        except (TelegramAPIError, asyncio.TimeoutError):
            logger.info("Не удалось обновить приглашение дуэли %s", duel_id)
    if accepted["started"]:
        messages = [(accepted["author_id"], f"✅ Вызов принят: @{escape(actor.username or '')}. Дуэль началась!")]
        if not edited:
            messages.append((actor.id, "Дуэль началась!"))
        for user_id, text in messages:
            try:
                await callback.bot.send_message(user_id, text, reply_markup=kb)
            except (TelegramAPIError, asyncio.TimeoutError):
                logger.warning("Не удалось отправить кнопку игроку дуэли %s", duel_id)
