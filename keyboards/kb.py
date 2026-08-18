"""Все inline-клавиатуры в одном месте, чтобы кнопки и callback_data не дублировались.

Сводка callback_data:
  check_sub                 — перепроверить подписку (гейт)
  go_menu                   — вернуться в главное меню
  menu_playlist / menu_fact / menu_chat / menu_tests / menu_events — пункты меню
  test_zodiac / quest_concert — выбор/перезапуск теста или квеста
  test_covers:<номер>       — запуск/перезапуск теста по обложкам (1, 2 или 3)
  test_musician              — запуск/перезапуск теста «Кто ты из рок/метал-музыкантов?»
  zodiac:<индекс>           — выбранный знак зодиака (0..11)
  cover_ans:<индекс>        — выбранный вариант ответа в тесте по обложкам
  cover_next                — перейти к следующей обложке (или к результату)
  muso_ans:<индекс>         — выбранный вариант ответа в тесте про музыкантов
  quest:<id_узла>           — переход к следующему узлу квеста
  broadcast_send / broadcast_cancel — подтверждение/отмена рассылки (админ, FSM Broadcast)

«Найди группу» — не callback, а кнопка web_app (Telegram Mini App), ссылка на
config.WEBAPP_URL + /rockle/; открывается вне диспетчера aiogram (см. server.py).
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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


def tests_menu_kb(rockle_url: str | None = None) -> InlineKeyboardMarkup:
    """rockle_url — ссылка на мини-игру «Найди группу» (config.WEBAPP_URL). Пусто —
    кнопка не показывается (например, при локальном запуске без HTTPS-адреса).
    """
    rows = [
        [InlineKeyboardButton(text="♈ Музыкант по знаку зодиака", callback_data="test_zodiac")],
        [InlineKeyboardButton(text="🖼 Угадай группу по обложке — Часть 1", callback_data="test_covers:1")],
        [InlineKeyboardButton(text="🖼 Угадай группу по обложке — Часть 2", callback_data="test_covers:2")],
        [InlineKeyboardButton(text="🖼 Угадай группу по обложке — Часть 3", callback_data="test_covers:3")],
        [InlineKeyboardButton(text="🎤 Кто ты из рок/метал-музыкантов?", callback_data="test_musician")],
        [InlineKeyboardButton(text="🎤 Квест «Спаси концерт»", callback_data="quest_concert")],
    ]
    if rockle_url:
        rows.append([InlineKeyboardButton(text="🧩 Найди группу (афиша дня)", web_app=WebAppInfo(url=rockle_url))])
    rows.append([_back_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def zodiac_kb() -> InlineKeyboardMarkup:
    """12 кнопок со знаками зодиака, по 2 в ряд (callback zodiac:<индекс>)."""
    builder = InlineKeyboardBuilder()
    for i, sign in enumerate(ZODIAC_SIGNS):
        builder.button(text=f"{ZODIAC_EMOJI[sign]} {sign}", callback_data=f"zodiac:{i}")
    builder.adjust(2)  # 12 знаков -> 6 рядов по 2
    builder.row(_back_button())  # кнопка «В меню» отдельным рядом
    return builder.as_markup()


def cover_options_kb(options: list) -> InlineKeyboardMarkup:
    """Кнопки вариантов (названия групп) в тесте по обложкам (callback cover_ans:<индекс>)."""
    builder = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        builder.button(text=opt, callback_data=f"cover_ans:{i}")
    builder.adjust(2)  # названия групп короткие — по 2 в ряд
    builder.row(_back_button())  # кнопка «В меню» отдельным рядом
    return builder.as_markup()


def cover_feedback_kb(is_last: bool) -> InlineKeyboardMarkup:
    """После ответа: перейти к следующей обложке или к результату (callback cover_next)."""
    nxt = "🏁 Результат" if is_last else "Дальше →"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=nxt, callback_data="cover_next")],
        [_back_button()],
    ])


def musician_options_kb(options: list) -> InlineKeyboardMarkup:
    """Кнопки вариантов ответа в тесте про музыкантов (callback muso_ans:<индекс>).

    Варианты — целые фразы, а не короткие названия, поэтому по одной в ряд.
    """
    builder = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        builder.button(text=opt, callback_data=f"muso_ans:{i}")
    builder.button(text="⬅ В меню", callback_data="go_menu")
    builder.adjust(1)
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


def covers_result_kb(key: str) -> InlineKeyboardMarkup:
    """Экран результата теста по обложкам: «Пройти заново» перезапускает ту же часть."""
    return _retry_kb(f"test_covers:{key}")


def musician_result_kb() -> InlineKeyboardMarkup:
    return _retry_kb("test_musician")


def quest_end_kb() -> InlineKeyboardMarkup:
    """Кнопки на карточке-концовке квеста."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Сыграть заново", callback_data="quest_concert")],
        [_back_button()],
    ])


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    """Подтверждение/отмена рассылки (админ, см. handlers/admin.py)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разослать", callback_data="broadcast_send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
