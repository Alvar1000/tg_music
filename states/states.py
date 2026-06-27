"""FSM-состояния для пошаговых сценариев."""
from aiogram.fsm.state import State, StatesGroup


class StyleTest(StatesGroup):
    """Тест «Какой стиль рока тебе подходит» (копим очки по стилям)."""
    answering = State()


class Quest(StatesGroup):
    """Квест «Спаси концерт» (храним текущий узел графа)."""
    playing = State()
