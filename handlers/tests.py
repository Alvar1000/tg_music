"""Тесты и квест: меню выбора, тест по зодиаку, тест на стиль (FSM) и квест-граф (FSM)."""
import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import config
from database import db
from handlers.menu import safe_edit
from keyboards.kb import (
    ZODIAC_SIGNS,
    quest_choices_kb,
    quest_end_kb,
    style_options_kb,
    style_result_kb,
    tests_menu_kb,
    zodiac_kb,
    zodiac_result_kb,
)
from states.states import Quest, StyleTest

logger = logging.getLogger(__name__)
router = Router()

# Показывается, если нужный контент-файл отсутствует или повреждён.
CONTENT_ERROR_TEXT = "Этот раздел пока не готов. Загляни чуть позже 🤘"


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


# ============ (б) Тест «Какой стиль тебе подходит» ============
@router.callback_query(F.data == "test_style")
async def style_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    quiz = config.load_content("quiz_style.json", default={})
    questions = quiz.get("questions") or []
    if not questions:
        await safe_edit(callback, CONTENT_ERROR_TEXT, style_result_kb())
        return

    # Начинаем тест: очки пустые, вопрос нулевой.
    await state.set_state(StyleTest.answering)
    await state.update_data(question=0, scores={})
    await _show_style_question(callback, quiz, 0)


async def _show_style_question(callback: CallbackQuery, quiz: dict, q_index: int) -> None:
    question = quiz["questions"][q_index]
    total = len(quiz["questions"])
    text = (
        f"🎸 <b>{html.escape(quiz.get('title', 'Тест'))}</b>\n"
        f"Вопрос {q_index + 1} из {total}\n\n"
        f"{html.escape(question['q'])}"
    )
    await safe_edit(callback, text, style_options_kb(question["options"]))


@router.callback_query(StyleTest.answering, F.data.startswith("style_ans:"))
async def style_answer(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    quiz = config.load_content("quiz_style.json", default={})
    questions = quiz.get("questions") or []

    data = await state.get_data()
    q_index = data.get("question", 0)
    scores = data.get("scores", {})
    option_index = int(callback.data.split(":")[1])

    # Защита от устаревших кнопок и изменившегося контента.
    if not questions or q_index >= len(questions) or option_index >= len(questions[q_index]["options"]):
        await state.clear()
        await safe_edit(callback, CONTENT_ERROR_TEXT, style_result_kb())
        return

    # Прибавляем очки выбранного варианта.
    chosen = questions[q_index]["options"][option_index]
    for style, points in chosen.get("scores", {}).items():
        scores[style] = scores.get(style, 0) + points

    q_index += 1
    if q_index < len(questions):
        await state.update_data(question=q_index, scores=scores)
        await _show_style_question(callback, quiz, q_index)
    else:
        await state.clear()
        await _show_style_result(callback, quiz, scores)


async def _show_style_result(callback: CallbackQuery, quiz: dict, scores: dict) -> None:
    results = quiz.get("results", {})
    if not scores or not results:
        await safe_edit(callback, CONTENT_ERROR_TEXT, style_result_kb())
        return

    best = max(scores, key=scores.get)  # стиль с максимумом очков
    result = results.get(best)
    if not result:
        await safe_edit(callback, CONTENT_ERROR_TEXT, style_result_kb())
        return

    await db.save_quiz_result(callback.from_user.id, "style", best)
    text = (
        f"Твой стиль: <b>{html.escape(result['title'])}</b>\n\n"
        f"{html.escape(result['desc'])}"
    )
    await safe_edit(callback, text, style_result_kb())


# ============ (в) Квест «Спаси концерт» ============
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
