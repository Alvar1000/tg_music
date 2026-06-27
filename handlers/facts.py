"""Рандомные факты о роке без повторов для каждого пользователя."""
import html
import logging
import random

from aiogram import F, Router
from aiogram.types import CallbackQuery

import config
from database import db
from handlers.menu import safe_edit
from keyboards.kb import back_to_menu_kb, fact_kb

logger = logging.getLogger(__name__)
router = Router()

ALL_FACTS_SEEN_TEXT = "Ты узнал все факты! Скоро добавим новые 🤘"
NO_FACTS_TEXT = "Факты пока не загружены. Загляни позже 🤘"


@router.callback_query(F.data == "menu_fact")
async def show_fact(callback: CallbackQuery) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    facts = config.load_content("facts.json", default=[])
    if not facts:
        await safe_edit(callback, NO_FACTS_TEXT, back_to_menu_kb())
        return

    seen = await db.get_seen_fact_ids(user_id)
    unseen = [f for f in facts if f["id"] not in seen]

    if not unseen:
        if config.RESET_FACTS_WHEN_DONE:
            await db.reset_seen_facts(user_id)
            unseen = facts  # начинаем круг заново
        else:
            await safe_edit(callback, ALL_FACTS_SEEN_TEXT, back_to_menu_kb())
            return

    fact = random.choice(unseen)
    await db.mark_fact_seen(user_id, fact["id"])

    text = f"🎲 <b>Факт о роке</b>\n\n{html.escape(fact['text'])}"
    await safe_edit(callback, text, fact_kb())
