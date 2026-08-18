"""Админ-команды и загрузка контента — доступны только id из ADMIN_IDS."""
import asyncio
import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

import config
from database import db
from keyboards.kb import broadcast_confirm_kb
from states.states import Broadcast

logger = logging.getLogger(__name__)
router = Router()

# Плейлисты — крошечный JSON; файлы крупнее точно не наш формат.
MAX_UPLOAD_BYTES = 1_000_000
# Ограничения длины полей, чтобы экран «Плейлист дня» гарантированно влезал
# в лимит сообщения Telegram (4096 символов).
MAX_TITLE_LEN = 200
MAX_DESC_LEN = 500


def _valid_url(value) -> bool:
    """Ссылка для inline-кнопки должна быть http(s) — иначе Telegram её отвергнет."""
    return str(value).strip().lower().startswith(("http://", "https://"))


def _normalize_playlist(item: dict) -> dict:
    """Оставляем только нужные поля, обрезаем пробелы и слишком длинные строки."""
    return {
        "title": str(item.get("title", "")).strip()[:MAX_TITLE_LEN],
        "desc": str(item.get("desc", "")).strip()[:MAX_DESC_LEN],
        "url": str(item.get("url", "")).strip(),
    }


# Человекочитаемые названия тестов (ключи — quiz_name из quiz_results).
TEST_LABELS = {
    "zodiac": "Рок-гороскоп",
    "covers_1": "Угадай группу по обложке (ч.1)",
    "covers_2": "Угадай группу по обложке (ч.2)",
    "covers_3": "Угадай группу по обложке (ч.3)",
    "musician": "Кто ты из рок/метал-музыкантов?",
    "quest_concert": "Спаси концерт (квест)",
}


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return  # не-админам не отвечаем

    stats = await db.get_stats()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total']}</b>\n"
        f"🆕 Новых за сегодня: <b>{stats['new_today']}</b>\n"
        f"✅ Сейчас подписаны: <b>{stats['subscribed']}</b>\n\n"
        "<b>Сегодня (UTC)</b>\n"
        f"👀 Заходили: <b>{stats['active_today']}</b>\n"
        f"🎵 Открытий «Плейлиста дня»: <b>{stats['playlist_today']}</b>\n"
        f"🧩 Прошли «Найди группу»: <b>{stats['rockle_today']}</b>\n"
    )

    tests_today = stats["tests_today"]
    if tests_today:
        text += "\n🧩 <b>Тесты сегодня</b> (завершённых прохождений):\n"
        # Известные тесты — в фиксированном порядке, затем всё прочее.
        shown = set()
        for key, label in TEST_LABELS.items():
            if key in tests_today:
                text += f"• {html.escape(label)}: <b>{tests_today[key]}</b>\n"
                shown.add(key)
        for key, count in tests_today.items():
            if key not in shown:
                text += f"• {html.escape(str(key))}: <b>{count}</b>\n"
    else:
        text += "\n🧩 Тестами сегодня ещё не пользовались."

    await message.answer(text)


# ============ Рассылка всем пользователям ============
# /broadcast -> админ шлёт произвольное сообщение (текст/фото/видео — что угодно)
# -> превью с подтверждением -> рассылаем через copy_message каждому user_id из
# таблицы users. copy_message сам переносит любой тип контента, поэтому не нужно
# разбирать текст/медиа руками. Обработчик черновика (Broadcast.awaiting_content)
# зарегистрирован ДО upload_playlists: если админ пришлёт документ в процессе
# рассылки, он должен уйти в черновик, а не в загрузчик плейлистов — aiogram
# проверяет хендлеры по порядку регистрации и берёт первый подходящий.

BROADCAST_DELAY = 0.05  # ~20 сообщений/сек — с запасом от лимитов Telegram
BROADCAST_PROGRESS_EVERY = 25


def _not_a_command(message: Message) -> bool:
    """Черновик рассылки не должен перехватывать другие команды (/stats и т.п.),
    случайно набранные админом, пока идёт диалог рассылки.
    """
    return not (message.text and message.text.startswith("/"))


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await state.set_state(Broadcast.awaiting_content)
    await message.answer(
        "📣 Пришли сообщение, которое разослать всем пользователям бота "
        "(текст, фото, видео — что угодно, разошлю как есть).\n\n"
        "Отменить — /cancel."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    if await state.get_state() in (Broadcast.awaiting_content, Broadcast.confirming):
        await state.clear()
        await message.answer("Рассылка отменена.")


@router.message(Broadcast.awaiting_content, _not_a_command)
async def broadcast_draft(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return
    total = len(await db.get_all_user_ids())
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Broadcast.confirming)
    await message.reply(
        f"Сообщение выше получат <b>{total}</b> пользователей. Разослать?",
        reply_markup=broadcast_confirm_kb(),
    )


@router.callback_query(Broadcast.confirming, F.data == "broadcast_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.")


@router.callback_query(Broadcast.confirming, F.data == "broadcast_send")
async def broadcast_send(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()
    src_chat_id = data.get("chat_id")
    src_message_id = data.get("message_id")
    if not src_chat_id or not src_message_id:
        await callback.message.edit_text("❌ Черновик рассылки потерян, начни заново: /broadcast")
        return

    user_ids = await db.get_all_user_ids()
    status_msg = await callback.message.edit_text(f"⏳ Рассылаю: 0/{len(user_ids)}…")

    sent = blocked = failed = 0
    for i, user_id in enumerate(user_ids, start=1):
        try:
            await bot.copy_message(chat_id=user_id, from_chat_id=src_chat_id, message_id=src_message_id)
            sent += 1
        except TelegramRetryAfter as e:
            # Telegram сам просит подождать — уважаем и пробуем ещё раз один раз.
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(chat_id=user_id, from_chat_id=src_chat_id, message_id=src_message_id)
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1  # пользователь заблокировал бота — пропускаем, не ошибка
        except TelegramAPIError as e:
            logger.warning("Рассылка: не смог отправить %s: %s", user_id, e)
            failed += 1

        if i % BROADCAST_PROGRESS_EVERY == 0 or i == len(user_ids):
            try:
                await status_msg.edit_text(f"⏳ Рассылаю: {i}/{len(user_ids)}…")
            except TelegramAPIError:
                pass
        await asyncio.sleep(BROADCAST_DELAY)

    await status_msg.edit_text(
        "✅ Рассылка завершена.\n\n"
        f"Доставлено: <b>{sent}</b>\n"
        f"Заблокировали бота: <b>{blocked}</b>\n"
        f"Ошибок: <b>{failed}</b>"
    )
    logger.info(
        "Админ %s разослал сообщение: доставлено %d, заблокировали %d, ошибок %d",
        callback.from_user.id, sent, blocked, failed,
    )


@router.message(F.document)
async def upload_playlists(message: Message, bot: Bot) -> None:
    """Админ присылает .json-файл — дописываем плейлисты в КОНЕЦ очереди.

    Очередь не заменяется целиком, только дополняется. Дубли по url отсекаются,
    запись атомарная (через временный файл), чтобы не повредить очередь на сбое.
    """
    if message.from_user.id not in config.ADMIN_IDS:
        return  # не-админам не отвечаем

    doc = message.document
    if not (doc.file_name or "").lower().endswith(".json"):
        await message.answer("Пришли <b>.json</b>-файл со списком плейлистов 🤘")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await message.answer("❌ Файл слишком большой — жду обычный JSON со списком плейлистов.")
        return

    # Скачиваем файл в память (ошибки сети/Telegram — не роняем хендлер).
    try:
        file = await bot.get_file(doc.file_id)
        raw = (await bot.download_file(file.file_path)).read()
    except TelegramAPIError as e:
        await message.answer(f"❌ Не смог скачать файл: {html.escape(str(e))}")
        return
    if len(raw) > MAX_UPLOAD_BYTES:  # на случай, если размер не был известен заранее
        await message.answer("❌ Файл слишком большой — жду обычный JSON со списком плейлистов.")
        return

    # Парсим JSON.
    try:
        items = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        await message.answer(f"❌ Не смог разобрать JSON: {html.escape(str(e))}")
        return

    # Валидация: непустой массив объектов, у каждого http(s)-url.
    if not isinstance(items, list) or not items:
        await message.answer(
            "❌ Ожидаю непустой массив объектов вида "
            "{\"title\", \"desc\", \"url\"}."
        )
        return
    if not all(isinstance(x, dict) and _valid_url(x.get("url", "")) for x in items):
        await message.answer(
            "❌ У каждого плейлиста должен быть <b>url</b>, "
            "начинающийся с http:// или https://."
        )
        return

    # Дозаписываем к существующим, отсекая дубли по url (и внутри файла тоже).
    current = config.load_playlists(default=[])
    if not isinstance(current, list):  # повреждённый/чужой файл — начинаем заново
        current = []
    seen = {str(x.get("url", "")).strip() for x in current}
    added = []
    for item in items:
        norm = _normalize_playlist(item)
        if norm["url"] in seen:
            continue
        seen.add(norm["url"])
        added.append(norm)
    current.extend(added)

    # Атомарная запись: пишем во временный файл и подменяем им основной.
    path = config.PLAYLISTS_PATH
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)  # на свежем диске папки может не быть
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        tmp.unlink(missing_ok=True)  # не оставляем осиротевший .tmp рядом с очередью
        await message.answer(f"❌ Не смог сохранить очередь: {html.escape(str(e))}")
        return

    skipped = len(items) - len(added)
    await message.answer(
        f"✅ Добавил плейлистов: <b>{len(added)}</b>"
        + (f" (пропущено дублей: {skipped})" if skipped else "")
        + f"\nВсего в очереди: <b>{len(current)}</b>."
    )
    logger.info(
        "Админ %s загрузил плейлисты: +%d (всего %d)",
        message.from_user.id, len(added), len(current),
    )


@router.message(Command("playlists"))
async def cmd_playlists(message: Message) -> None:
    """Статус очереди плейлистов: сколько всего и какой показывается сейчас."""
    if message.from_user.id not in config.ADMIN_IDS:
        return

    playlists = config.load_playlists(default=[])
    if not playlists:
        await message.answer(
            "Очередь пуста. Пришли .json-файл (массив объектов "
            "{\"title\", \"desc\", \"url\"}), чтобы добавить плейлисты."
        )
        return

    index, last_advance = await db.get_playlist_pointer()
    index = min(index, len(playlists) - 1)
    current = playlists[index]
    await message.answer(
        "🎵 <b>Очередь плейлистов</b>\n\n"
        f"Всего: <b>{len(playlists)}</b>\n"
        f"Сейчас показывается: <b>#{index + 1}</b> — "
        f"{html.escape(str(current.get('title', '—')))}\n"
        f"Последнее продвижение: {last_advance or '—'}"
    )


@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    """Присылает админу целостную копию БД файлом прямо в чат — это и есть бэкап."""
    if message.from_user.id not in config.ADMIN_IDS:
        return

    # Копию кладём рядом с боевой базой (та же ФС/диск), отправляем и удаляем.
    dest = Path(config.DB_PATH).with_name("backup.db")
    dest.unlink(missing_ok=True)  # VACUUM INTO не пишет в уже существующий файл
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    try:
        await db.backup_database(str(dest))
        await message.answer_document(
            FSInputFile(str(dest), filename=f"bot-{stamp}.db"),
            caption="📦 Бэкап базы. Сохрани файл у себя — это и есть резервная копия.",
        )
        logger.info("Админ %s сделал бэкап БД", message.from_user.id)
    except Exception as e:  # noqa: BLE001 — утилита, не должна ронять бота
        logger.exception("Бэкап БД не удался")
        await message.answer(f"❌ Не смог сделать бэкап: {html.escape(str(e))}")
    finally:
        dest.unlink(missing_ok=True)
