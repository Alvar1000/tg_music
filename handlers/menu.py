"""Главное меню, простые экраны-ссылки (плейлист, чат) и общие UI-помощники.

Главное меню может показываться с картинкой-баннером (файл content/menu.jpg или
переменная MENU_IMAGE). Фото-сообщение нельзя «отредактировать» в текстовое и
наоборот, поэтому при переходе меню <-> подэкран мы удаляем старое сообщение и
шлём новое; переходы текст -> текст по-прежнему редактируются на месте.
"""
import html
import logging
import random
from datetime import date, datetime, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

import config
from database import db
from keyboards.kb import back_to_menu_kb, link_kb, main_menu_kb

logger = logging.getLogger(__name__)
router = Router()

WELCOME_TEXT = (
    "🤘 Добро пожаловать в <b>Black Magic Woman</b>!\n\n"
    "Здесь — плейлисты, факты о роке, тесты и квест. Выбирай:"
)

MENU_TEXT = "🎸 <b>Главное меню</b>\n\nЧем займёмся?"

# file_id баннера кешируется после первой отправки, чтобы не грузить файл каждый раз.
_banner_file_id: str | None = None


def _banner_source():
    """Картинка для меню: file_id из кеша, URL или файл с диска. None — если нет."""
    if _banner_file_id:
        return _banner_file_id
    value = (config.MENU_IMAGE or "").strip()
    if value.startswith("http"):
        return value  # картинка по URL
    # ищем локальный файл: сначала заданный путь, затем content/menu.{jpg,png,...}
    candidates = [Path(value)] if value else []
    for ext in ("jpg", "jpeg", "png", "webp"):
        candidates.append(config.CONTENT_DIR / f"menu.{ext}")
    for path in candidates:
        if path.exists():
            return FSInputFile(str(path))
    return None


async def _send_photo(target: Message, banner, caption: str, reply_markup) -> None:
    """Шлёт фото-меню и кеширует file_id баннера после первой загрузки.

    Если отправка фото падает (битая картинка, сеть, удалённый чат) — деградируем
    до текстового меню без баннера, чтобы пользователь не остался без ответа.
    """
    global _banner_file_id
    try:
        sent = await target.answer_photo(banner, caption=caption, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logger.warning("Не удалось отправить баннер меню: %s", e)
        try:
            await target.answer(caption, reply_markup=reply_markup)
        except TelegramBadRequest as e2:
            logger.warning("Не удалось отправить текст меню даже без баннера: %s", e2)
        return
    if _banner_file_id is None and sent.photo:
        _banner_file_id = sent.photo[-1].file_id


async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """Показывает текстовый экран под кнопкой.

    Если текущее сообщение — фото (меню с баннером), его нельзя превратить в текст
    редактированием, поэтому удаляем и шлём новое. Иначе редактируем на месте
    (с защитой от ошибки «message is not modified»).
    """
    msg = callback.message

    async def _answer(markup) -> None:
        # Шлём новое сообщение, не роняя хендлер на ошибке Telegram (например,
        # битый url в кнопке или слишком длинный текст). В крайнем случае —
        # без клавиатуры, чтобы пользователь хотя бы увидел текст.
        try:
            await msg.answer(text, reply_markup=markup)
        except TelegramBadRequest as e:
            logger.warning("Не удалось отправить экран: %s", e)
            if markup is not None:
                try:
                    await msg.answer(text)
                except TelegramBadRequest as e2:
                    logger.warning("Не удалось отправить экран даже без кнопок: %s", e2)

    if msg.photo:
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
        await _answer(reply_markup)
        return
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await _answer(reply_markup)


async def show_main_menu(event: Message | CallbackQuery, greeting: bool = False) -> None:
    """Показывает главное меню — с картинкой-баннером, если она задана."""
    text = WELCOME_TEXT if greeting else MENU_TEXT
    kb = main_menu_kb()
    banner = _banner_source()

    # /start — событие Message: просто шлём новое сообщение.
    if isinstance(event, Message):
        if banner is None:
            await event.answer(text, reply_markup=kb)
        else:
            await _send_photo(event, banner, text, kb)
        return

    # Callback: без баннера — обычное текстовое меню.
    msg = event.message
    if banner is None:
        await safe_edit(event, text, kb)
        return
    # Баннер есть. Текущим сообщением может быть и фото-меню, и фото-вопрос теста
    # по обложкам — поэтому не редактируем подпись (рискуем оставить чужую
    # картинку под текстом меню), а удаляем и шлём баннер заново.
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    await _send_photo(msg, banner, text, kb)


@router.callback_query(F.data == "go_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # сбрасываем незаконченный тест/квест
    await callback.answer()
    await show_main_menu(callback)


@router.callback_query(F.data == "menu_playlist")
async def show_playlist(callback: CallbackQuery) -> None:
    await callback.answer()
    await db.log_feature(callback.from_user.id, "playlist")
    playlists = config.load_playlists(default=[])

    # Очереди ещё нет (плейлисты заливает админ файлом через бота) — запасной экран.
    if not playlists:
        text = (
            "🎵 <b>Плейлист дня</b>\n\n"
            "Свежая подборка рока на сегодня. Жми кнопку и врубай погромче!"
        )
        if config.YANDEX_PLAYLIST_URL:
            await safe_edit(callback, text, link_kb("▶️ Слушать на Яндекс.Музыке", config.YANDEX_PLAYLIST_URL))
        else:
            await safe_edit(callback, text + "\n\n<i>Ссылка пока не задана.</i>", back_to_menu_kb())
        return

    # Плейлист дня — общий для всех. Указатель сдвигается на +1 за каждый
    # прошедший календарный день и клэмпится к длине очереди — при исчерпании
    # очереди он «встаёт на паузу» на последнем индексе вместо того, чтобы
    # улетать вперёд по календарным дням. Благодаря паузе, когда админ дольёт
    # новые плейлисты, показ продолжится СЛЕДУЮЩИМ по очереди — ни один не
    # проскочит, сколько бы дней очередь ни простаивала пустой.
    index, last_advance = await db.get_playlist_pointer()
    # День считаем по UTC — как и все остальные даты в БД (см. db._now).
    today = datetime.now(timezone.utc).date().isoformat()
    if last_advance is None or today < last_advance:
        # Первый показ либо часы сервера ушли назад — фиксируем старт, индекс не трогаем.
        await db.set_playlist_pointer(index, today)
    elif today > last_advance:
        days = (date.fromisoformat(today) - date.fromisoformat(last_advance)).days
        index = min(index + days, len(playlists) - 1)
        await db.set_playlist_pointer(index, today)

    if index < len(playlists) - 1:
        pl = playlists[index]
    else:
        # Дошли до конца очереди — вместо залипания на последнем весь день
        # показываем случайный из уже показанных. Сид — сегодняшняя дата, так
        # что в течение дня всем пользователям достаётся один и тот же плейлист.
        pl = random.Random(today).choice(playlists)
    title = html.escape(str(pl.get("title", "Плейлист дня")))
    desc = html.escape(str(pl.get("desc", "")))
    text = f"🎵 <b>Плейлист дня</b>\n\n<b>{title}</b>"
    if desc:
        text += f"\n{desc}"
    await safe_edit(callback, text, link_kb("▶️ Слушать", pl["url"]))


@router.callback_query(F.data == "menu_chat")
async def show_chat(callback: CallbackQuery) -> None:
    await callback.answer()
    text = (
        "💬 <b>Общение</b>\n\n"
        "Заходи в чат сообщества — обсудить любимые группы, концерты и находки."
    )
    if config.COMMUNITY_CHAT_URL:
        await safe_edit(callback, text, link_kb("💬 Перейти в чат", config.COMMUNITY_CHAT_URL))
    else:
        await safe_edit(callback, text + "\n\n<i>Ссылка пока не задана.</i>", back_to_menu_kb())
