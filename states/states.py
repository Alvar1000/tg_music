"""FSM-состояния для пошаговых сценариев."""
from aiogram.fsm.state import State, StatesGroup


class CoverQuiz(StatesGroup):
    """Тест «Угадай группу по обложке» (храним текущий вопрос, счёт и фазу)."""
    answering = State()


class MusicianQuiz(StatesGroup):
    """Тест «Кто ты из рок/метал-музыкантов?» (храним текущий вопрос и набранные очки)."""
    answering = State()


class Quest(StatesGroup):
    """Квест «Спаси концерт» (храним текущий узел графа)."""
    playing = State()
