"""Тесты и квест: меню выбора, тест по зодиаку, тест по обложкам (FSM) и квест-граф (FSM)."""
import html
import logging
import random

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile

import config
from database import db
from handlers.menu import safe_edit
from keyboards.kb import (
    ZODIAC_SIGNS,
    cover_feedback_kb,
    cover_options_kb,
    covers_result_kb,
    musician_options_kb,
    musician_result_kb,
    quest_choices_kb,
    quest_end_kb,
    tests_menu_kb,
    zodiac_kb,
    zodiac_result_kb,
)
from states.states import CoverQuiz, MusicianQuiz, Quest

logger = logging.getLogger(__name__)
router = Router()

# Показывается, если нужный контент-файл отсутствует или повреждён.
CONTENT_ERROR_TEXT = "Этот раздел пока не готов. Загляни чуть позже 🤘"

# Кеш file_id обложек: после первой загрузки шлём по file_id, не перезаливая файл.
_cover_file_id_cache: dict[str, str] = {}


# --- Подменю выбора теста ---
@router.callback_query(F.data == "menu_tests")
async def tests_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await safe_edit(callback, "🧠 <b>Тесты</b>\n\nВыбирай:", tests_menu_kb())


# ============ (а) Тест «Музыкант по знаку зодиака» ============
@router.callback_query(F.data == "test_zodiac")
async def zodiac_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await safe_edit(callback, "♈ <b>Музыкант по знаку зодиака</b>\n\nВыбери свой знак:", zodiac_kb())


@router.callback_query(F.data.startswith("zodiac:"))
async def zodiac_result(callback: CallbackQuery) -> None:
    await callback.answer()
    index = int(callback.data.split(":")[1])
    sign = ZODIAC_SIGNS[index]

    data = config.load_content("quiz_zodiac.json", default={})
    item = data.get(sign)
    if not item:
        await safe_edit(callback, CONTENT_ERROR_TEXT, zodiac_result_kb())
        return

    await db.save_quiz_result(callback.from_user.id, "zodiac", sign)
    text = (
        f"{sign} — это <b>{html.escape(item['name'])}</b>\n\n"
        f"{html.escape(item['desc'])}"
    )
    await safe_edit(callback, text, zodiac_result_kb())


# ============ (б) Тест «Угадай группу по обложке» ============
# Три теста по обложкам (quiz_covers_1/2/3.json; части 1 и 2 — по 15 вопросов,
# часть 3 — 16). Каждый вопрос — это фото обложки с вариантами-кнопками. Данные
# читаются по key из callback_data, так что новую часть добавляют файлом + кнопкой
# в меню, без правок в коде. Так как фото-сообщение нельзя
# превратить в текст (и наоборот) редактированием, вопросы шлём как новые фото
# (старое удаляем), а разбор ответа делаем edit_caption поверх той же обложки.


def _resolve_cover(photo_name: str):
    """Источник для отправки обложки: file_id из кеша, иначе локальный файл. None — если нет."""
    if not photo_name:
        return None
    cached = _cover_file_id_cache.get(photo_name)
    if cached:
        return cached
    path = config.COVERS_DIR / photo_name
    if path.exists():
        return FSInputFile(str(path))
    return None


@router.callback_query(F.data.startswith("test_covers:"))
async def covers_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    key = callback.data.split(":")[1]
    quiz = config.load_content(f"quiz_covers_{key}.json", default={})
    questions = quiz.get("questions") or []
    if not questions:
        await safe_edit(callback, CONTENT_ERROR_TEXT, covers_result_kb(key))
        return

    # Начинаем тест: счёт ноль, вопрос нулевой, фаза «ждём ответ».
    await state.set_state(CoverQuiz.answering)
    await state.update_data(quiz=key, question=0, score=0, phase="question")
    await _send_cover_question(callback, state, quiz, key, 0)


async def _send_cover_question(
    callback: CallbackQuery, state: FSMContext, quiz: dict, key: str, q_index: int
) -> None:
    """Удаляет текущее сообщение и шлёт новую обложку-вопрос (с кешем file_id)."""
    question = quiz["questions"][q_index]
    total = len(quiz["questions"])
    photo = question.get("photo", "")
    src = _resolve_cover(photo)
    if src is None:  # файла обложки нет — не оставляем игрока в тупике
        logger.warning("Нет файла обложки: %s", photo)
        await state.clear()
        await safe_edit(callback, CONTENT_ERROR_TEXT, covers_result_kb(key))
        return

    caption = (
        f"🖼 <b>{html.escape(quiz.get('title', 'Угадай группу по обложке'))}</b>\n"
        f"Вопрос {q_index + 1} из {total}\n\n"
        f"Чья это обложка?"
    )
    msg = callback.message
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    try:
        sent = await msg.answer_photo(
            src, caption=caption, reply_markup=cover_options_kb(question.get("options", []))
        )
    except TelegramBadRequest as e:
        logger.warning("Не удалось отправить обложку %s: %s", photo, e)
        await state.clear()
        # Сама отправка заглушки тоже может упасть — деградируем как в safe_edit().
        try:
            await msg.answer(CONTENT_ERROR_TEXT, reply_markup=covers_result_kb(key))
        except TelegramBadRequest as e2:
            logger.warning("Не удалось отправить заглушку обложки: %s", e2)
            try:
                await msg.answer(CONTENT_ERROR_TEXT)
            except TelegramBadRequest as e3:
                logger.warning("Не удалось отправить заглушку даже без кнопок: %s", e3)
        return

    # Кешируем file_id после первой реальной загрузки файла.
    if isinstance(src, FSInputFile) and sent.photo:
        _cover_file_id_cache[photo] = sent.photo[-1].file_id
    await state.update_data(quiz=key, question=q_index, phase="question")


@router.callback_query(CoverQuiz.answering, F.data.startswith("cover_ans:"))
async def cover_answer(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if data.get("phase") != "question":
        return  # на этот вопрос уже ответили — игнорируем повторный тап

    key = data.get("quiz")
    quiz = config.load_content(f"quiz_covers_{key}.json", default={})
    questions = quiz.get("questions") or []
    q_index = data.get("question", 0)
    score = data.get("score", 0)
    chosen = int(callback.data.split(":")[1])

    # Защита от устаревших кнопок и изменившегося контента.
    if not questions or q_index >= len(questions) or chosen >= len(questions[q_index].get("options", [])):
        await state.clear()
        await safe_edit(callback, CONTENT_ERROR_TEXT, covers_result_kb(key))
        return

    question = questions[q_index]
    correct = question.get("correct", 0)
    is_right = chosen == correct
    if is_right:
        score += 1

    total = len(questions)
    answered = q_index + 1
    answer_name = question["options"][correct] if correct < len(question["options"]) else question.get("group", "")
    verdict = "✅ Верно!" if is_right else "❌ Мимо."
    reveal = f"Это <b>{html.escape(str(answer_name))}</b>"
    album = question.get("album")
    if album:
        reveal += f" — «{html.escape(str(album))}»"
    caption = (
        f"🖼 <b>{html.escape(quiz.get('title', 'Угадай группу по обложке'))}</b>\n"
        f"Вопрос {answered} из {total}\n\n"
        f"{verdict}\n{reveal}\n\n"
        f"Счёт: <b>{score}/{answered}</b>"
    )

    await state.update_data(score=score, phase="feedback")
    try:
        await callback.message.edit_caption(
            caption=caption, reply_markup=cover_feedback_kb(answered >= total)
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.warning("Не удалось показать разбор обложки: %s", e)


@router.callback_query(CoverQuiz.answering, F.data == "cover_next")
async def cover_next(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if data.get("phase") != "feedback":
        return  # «Дальше» жмут только после разбора ответа

    key = data.get("quiz")
    quiz = config.load_content(f"quiz_covers_{key}.json", default={})
    questions = quiz.get("questions") or []
    score = data.get("score", 0)
    next_index = data.get("question", 0) + 1

    if not questions:
        await state.clear()
        await safe_edit(callback, CONTENT_ERROR_TEXT, covers_result_kb(key))
        return

    if next_index < len(questions):
        await _send_cover_question(callback, state, quiz, key, next_index)
    else:
        await state.clear()
        await _show_cover_result(callback, quiz, key, score, len(questions))


async def _show_cover_result(
    callback: CallbackQuery, quiz: dict, key: str, score: int, total: int
) -> None:
    """Итог теста по обложкам: сохраняем результат и показываем счёт со званием."""
    await db.save_quiz_result(callback.from_user.id, f"covers_{key}", f"{score}/{total}")
    title = html.escape(quiz.get("title", "Угадай группу по обложке"))
    text = (
        f"🖼 <b>{title}</b>\n\n"
        f"Твой результат: <b>{score} из {total}</b>\n\n"
        f"{_cover_rank(score, total)}"
    )
    await safe_edit(callback, text, covers_result_kb(key))


def _cover_rank(score: int, total: int) -> str:
    """Шуточное звание по доле верных ответов."""
    if total <= 0:
        return ""
    ratio = score / total
    if ratio >= 1:
        return "🤘 Идеально! Ты — ходячая рок-энциклопедия."
    if ratio >= 0.8:
        return "🔥 Мощно! Почти все обложки узнал."
    if ratio >= 0.6:
        return "🎸 Крепко! Виниловый стаж чувствуется."
    if ratio >= 0.4:
        return "🙂 Неплохо, но есть куда копать."
    if ratio >= 0.2:
        return "😏 Для разогрева сойдёт — слушай больше."
    return "😅 Пора заряжать вертушку и навёрстывать!"


# ============ (в) Тест «Кто ты из рок/метал-музыкантов?» ============
# 10 вопросов, у каждого варианта ответа — свой музыкант (quiz_musician.json:
# questions[].options[].result). За каждый ответ музыкант получает +1 очко;
# после последнего вопроса побеждает тот, у кого очков больше (ничьи бьёт
# random.choice). Вопросы — просто текст с кнопками-вариантами, поэтому
# экраны редактируются на месте через safe_edit (в отличие от теста по
# обложкам, где вопрос — это фото).


@router.callback_query(F.data == "test_musician")
async def musician_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    quiz = config.load_content("quiz_musician.json", default={})
    questions = quiz.get("questions") or []
    if not questions or not quiz.get("results"):
        await safe_edit(callback, CONTENT_ERROR_TEXT, musician_result_kb())
        return

    await state.set_state(MusicianQuiz.answering)
    await state.update_data(question=0, scores={})
    await _send_musician_question(callback, state, quiz, 0)


async def _send_musician_question(
    callback: CallbackQuery, state: FSMContext, quiz: dict, q_index: int
) -> None:
    question = quiz["questions"][q_index]
    total = len(quiz["questions"])
    options = [opt.get("text", "") for opt in question.get("options", [])]
    text = (
        f"🎤 <b>{html.escape(quiz.get('title', 'Кто ты из рок/метал-музыкантов?'))}</b>\n"
        f"Вопрос {q_index + 1} из {total}\n\n"
        f"{html.escape(question.get('text', ''))}"
    )
    await safe_edit(callback, text, musician_options_kb(options))
    await state.update_data(question=q_index)


@router.callback_query(MusicianQuiz.answering, F.data.startswith("muso_ans:"))
async def musician_answer(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    quiz = config.load_content("quiz_musician.json", default={})
    questions = quiz.get("questions") or []
    data = await state.get_data()
    q_index = data.get("question", 0)
    scores = dict(data.get("scores", {}))
    chosen = int(callback.data.split(":")[1])

    # Защита от устаревших кнопок и изменившегося контента.
    if not questions or q_index >= len(questions) or chosen >= len(questions[q_index].get("options", [])):
        await state.clear()
        await safe_edit(callback, CONTENT_ERROR_TEXT, musician_result_kb())
        return

    result_key = questions[q_index]["options"][chosen].get("result")
    if result_key:
        scores[result_key] = scores.get(result_key, 0) + 1

    next_index = q_index + 1
    if next_index < len(questions):
        await state.update_data(question=next_index, scores=scores)
        await _send_musician_question(callback, state, quiz, next_index)
    else:
        await state.clear()
        await _show_musician_result(callback, quiz, scores)


async def _show_musician_result(callback: CallbackQuery, quiz: dict, scores: dict) -> None:
    """Итог теста: побеждает музыкант с наибольшим числом очков (ничьи — случайно)."""
    results = quiz.get("results", {})
    if not scores or not results:
        await safe_edit(callback, CONTENT_ERROR_TEXT, musician_result_kb())
        return

    top_score = max(scores.values())
    candidates = [key for key, score in scores.items() if score == top_score and key in results]
    if not candidates:
        await safe_edit(callback, CONTENT_ERROR_TEXT, musician_result_kb())
        return
    winner_key = random.choice(candidates)
    winner = results[winner_key]

    await db.save_quiz_result(callback.from_user.id, "musician", winner_key)
    emoji = winner.get("emoji", "🎸")
    name = html.escape(str(winner.get("name", "")))
    desc = html.escape(str(winner.get("desc", "")))
    text = (
        f"🎤 <b>{html.escape(quiz.get('title', 'Кто ты из рок/метал-музыкантов?'))}</b>\n\n"
        f"Ты — {emoji} <b>{name}</b>\n\n{desc}"
    )
    await safe_edit(callback, text, musician_result_kb())


# ============ (г) Квест «Спаси концерт» ============
@router.callback_query(F.data == "quest_concert")
async def quest_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    quest = config.load_content("quest_concert.json", default={})
    nodes = quest.get("nodes", {})
    start_node = quest.get("start")
    if not start_node or start_node not in nodes:
        await safe_edit(callback, CONTENT_ERROR_TEXT, quest_end_kb())
        return

    await state.set_state(Quest.playing)
    await _render_quest_node(callback, state, nodes, start_node)


@router.callback_query(Quest.playing, F.data.startswith("quest:"))
async def quest_step(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    quest = config.load_content("quest_concert.json", default={})
    nodes = quest.get("nodes", {})
    next_node = callback.data.split(":", 1)[1]

    if next_node not in nodes:
        await state.clear()
        await safe_edit(callback, CONTENT_ERROR_TEXT, quest_end_kb())
        return

    await _render_quest_node(callback, state, nodes, next_node)


async def _render_quest_node(callback: CallbackQuery, state: FSMContext, nodes: dict, node_id: str) -> None:
    """Универсальный движок графа: рисует узел. На концовке — завершает квест.

    Сюжетный узел берёт варианты из `choices`. Если их нет, но есть `next` —
    это нода-проходка: показываем одну кнопку «Дальше →».
    """
    node = nodes[node_id]
    if node.get("ending"):
        await _finish_quest(callback, state, nodes, node_id, node)
        return

    await state.update_data(node=node_id)
    choices = node.get("choices")
    if not choices and node.get("next"):
        choices = [{"text": "Дальше →", "next": node["next"]}]
    text = html.escape(node.get("text", ""))
    await safe_edit(callback, text, quest_choices_kb(choices or []))


async def _finish_quest(callback: CallbackQuery, state: FSMContext, nodes: dict, ending_id: str, node: dict) -> None:
    """Концовка: сохраняем результат, обновляем счётчик и показываем карточку."""
    await state.clear()
    user_id = callback.from_user.id
    await db.save_quiz_result(user_id, "quest_concert", ending_id)
    await db.mark_ending_seen(user_id, ending_id)

    ending_ids = [nid for nid, n in nodes.items() if n.get("ending")]
    opened = await db.count_seen_endings(user_id, ending_ids)
    total = len(ending_ids)
    await safe_edit(callback, _format_ending(node, opened, total), quest_end_kb())


def _format_ending(node: dict, opened: int, total: int) -> str:
    """Собирает карточку-концовку: заголовок, текст, вердикт/звание/редкость и прогресс."""
    parts = []
    if node.get("title"):
        parts.append(f"🏁 <b>{html.escape(str(node['title']))}</b>")
    if node.get("text"):
        parts.append(html.escape(str(node["text"])))

    meta = []
    if node.get("verdict"):
        meta.append(f"🎯 <b>Вердикт:</b> {html.escape(str(node['verdict']))}")
    if node.get("rank"):
        meta.append(f"🏆 <b>Звание:</b> {html.escape(str(node['rank']))}")
    rarity = html.escape(str(node.get("rarity", ""))).strip()
    score = html.escape(str(node.get("score", ""))).strip()
    if rarity or score:
        sep = " · " if rarity and score else ""
        meta.append(f"🃏 <b>Редкость:</b> {rarity}{sep}{score}")
    if meta:
        parts.append("\n".join(meta))

    if total:
        progress = f"📒 Открыто концовок: <b>{opened}/{total}</b>"
        if opened >= total:
            progress += "\n🎉 Ты собрал все концовки! Уважение 🤘"
        parts.append(progress)

    return "\n\n".join(parts)
