"""Фолбэк для устаревших кнопок.

Регистрируется ПОСЛЕДНИМ, поэтому сюда попадают только те нажатия, которые не
поймал ни один другой хендлер — например, кнопка из старого сообщения после
того, как тест или квест уже завершён и FSM-состояние сброшено.

Без этого хендлера такой callback остаётся без ответа, и Telegram крутит
«часик» на кнопке до таймаута. Здесь мы просто гасим его коротким сообщением.
"""
import logging

from aiogram import Router
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела — открой раздел заново 🤘", show_alert=False)
