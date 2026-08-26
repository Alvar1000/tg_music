"""Раздел «Мероприятия» — список из content/events.json."""
import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

import config
from handlers.menu import safe_edit
from keyboards.kb import back_to_menu_kb

logger = logging.getLogger(__name__)
router = Router()

NO_EVENTS_TEXT = (
    "📅 <b>Мероприятия</b>\n\n"
    "Пока ничего не запланировано. Следи за анонсами в канале 🤘"
)


@router.callback_query(F.data == "menu_events")
async def show_events(callback: CallbackQuery) -> None:
    await callback.answer()
    events = config.load_content("events.json", default=[])
    if not events:
        await safe_edit(callback, NO_EVENTS_TEXT, back_to_menu_kb())
        return

    lines = ["📅 <b>Ближайшие мероприятия</b>\n"]
    for ev in events:
        title = html.escape(str(ev.get("title", "")))
        date = html.escape(str(ev.get("date", "")))
        place = html.escape(str(ev.get("place", "")))
        desc = html.escape(str(ev.get("desc", "")))
        url = str(ev.get("url", "")).strip()

        # Заголовок: ссылкой, если задан url.
        if url:
            lines.append(f'🎤 <b><a href="{html.escape(url)}">{title}</a></b>')
        else:
            lines.append(f"🎤 <b>{title}</b>")
        if date:
            lines.append(f"🗓 {date}")
        if place:
            lines.append(f"📍 {place}")
        if desc:
            lines.append("")
            lines.append(desc)
        lines.append("")  # пустая строка между событиями

    text = "\n".join(lines).strip()
    await safe_edit(callback, text, back_to_menu_kb())
