"""Все inline-клавиатуры в одном месте, чтобы кнопки и callback_data не дублировались.

Сводка callback_data:
  check_sub                 — перепроверить подписку (гейт)
  go_menu                   — вернуться в главное меню
  menu_playlist / menu_fact / menu_chat / menu_tests / menu_events — пункты меню
  test_zodiac / test_style / quest_concert — выбор/перезапуск теста или квеста
  zodiac:<индекс>           — выбранный знак зодиака (0..11)
  style_ans:<индекс>        — выбранный вариант ответа в тесте на стиль
  quest:<id_узла>           — переход к следующему узлу квеста
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Канонический порядок знаков зодиака. Индекс в этом списке едет в callback_data.
ZODIAC_SIGNS = [
    "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
    "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы",
]

# Эмодзи к знакам — только для красоты кнопок.
ZODIAC_EMOJI = {
    "Овен": "♈", "Телец": "♉", "Близнецы": "♊", "Рак": "♋",
    "Лев": "♌", "Дева": "♍", "Весы": "♎", "Скорпион": "♏",
    "Стрелец": "♐", "Козерог": "♑", "Водолей": "♒", "Рыбы": "♓",
}


def _back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅ В меню", callback_data="go_menu")


def gate_kb(channel_url: str) -> InlineKeyboardMarkup:
    """Экран-гейт: подписаться и перепроверить подписку."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=channel_url)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Плейлист дня", callback_data="menu_playlist")],
        [InlineKeyboardButton(text="🎲 Рандомный факт", callback_data="menu_fact")],
        [InlineKeyboardButton(text="🧠 Тесты", callback_data="menu_tests")],
        [InlineKeyboardButton(text="📅 Мероприятия", callback_data="menu_events")],
        [InlineKeyboardButton(text="💬 Общение", callback_data="menu_chat")],
    ])


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_back_button()]])


def link_kb(text: str, url: str) -> InlineKeyboardMarkup:
    """Кнопка-ссылка + возврат в меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, url=url)],
        [_back_button()],
    ])


def fact_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Ещё факт", callback_data="menu_fact")],
        [_back_button()],
    ])


def tests_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♈ Музыкант по знаку зодиака", callback_data="test_zodiac")],
        [InlineKeyboardButton(text="🎸 Какой стиль тебе подходит", callback_data="test_style")],
        [InlineKeyboardButton(text="🎤 Квест «Спаси концерт»", callback_data="quest_concert")],
        [_back_button()],
    ])


def zodiac_kb() -> InlineKeyboardMarkup:
    """12 кнопок со знаками зодиака, по 2 в ряд (callback zodiac:<индекс>)."""
    builder = InlineKeyboardBuilder()
    for i, sign in enumerate(ZODIAC_SIGNS):
        builder.button(text=f"{ZODIAC_EMOJI[sign]} {sign}", callback_data=f"zodiac:{i}")
    builder.adjust(2)  # 12 знаков -> 6 рядов по 2
    builder.row(_back_button())  # кнопка «В меню» отдельным рядом
    return builder.as_markup()


def style_options_kb(options: list) -> InlineKeyboardMarkup:
    """Кнопки вариантов ответа текущего вопроса (callback style_ans:<индекс>)."""
    builder = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        builder.button(text=opt["text"], callback_data=f"style_ans:{i}")
    builder.button(text="⬅ В меню", callback_data="go_menu")
    builder.adjust(1)  # по одной кнопке в ряд (тексты длинные)
    return builder.as_markup()


def quest_choices_kb(choices: list) -> InlineKeyboardMarkup:
    """Кнопки вариантов на узле квеста (callback quest:<id_следующего_узла>)."""
    builder = InlineKeyboardBuilder()
    for choice in choices:
        builder.button(text=choice["text"], callback_data=f"quest:{choice['next']}")
    builder.button(text="⬅ В меню", callback_data="go_menu")
    builder.adjust(1)
    return builder.as_markup()


def _retry_kb(retry_callback: str) -> InlineKeyboardMarkup:
    """Стандартная пара кнопок на экране результата: «Пройти заново» + «В меню»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Пройти заново", callback_data=retry_callback)],
        [_back_button()],
    ])


def zodiac_result_kb() -> InlineKeyboardMarkup:
    return _retry_kb("test_zodiac")


def style_result_kb() -> InlineKeyboardMarkup:
    return _retry_kb("test_style")


def quest_end_kb() -> InlineKeyboardMarkup:
    """Кнопки на карточке-концовке квеста."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Сыграть заново", callback_data="quest_concert")],
        [_back_button()],
    ])
