"""Подсчёт итога рок-дуэли по сохранённым сервером ответам."""
from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor


@dataclass(frozen=True)
class TaskScore:
    """points=None — задание не закончено; attempts учитывает и неверные ответы."""

    points: int | None = None
    elapsed_ms: int = 0
    attempts: int = 0


def calculate_result(
    players: dict[int, list[TaskScore]],
    *,
    expired: bool = False,
    max_task_ms: int = 120_000,
) -> dict:
    """Возвращает active, cancelled или finished и итог со стороны обоих игроков.

    В списке каждого игрока должны быть все задания, включая неоткрытые.
    Время и очки поступают из серверного состояния, а не из запроса Mini App.
    Вызывать после проверки срока; expired означает истечение срока игры.
    """
    if len(players) != 2 or max_task_ms <= 0:
        raise ValueError("Нужны два игрока и положительный предел времени")
    sizes = {len(tasks) for tasks in players.values()}
    if len(sizes) != 1 or 0 in sizes:
        raise ValueError("У игроков должен быть одинаковый непустой набор заданий")
    for tasks in players.values():
        for task in tasks:
            if task.elapsed_ms < 0 or task.attempts < 0:
                raise ValueError("Время и число попыток не могут быть отрицательными")
            if task.points is not None and task.points < 0:
                raise ValueError("Очки не могут быть отрицательными")

    tasks_all = [task for tasks in players.values() for task in tasks]
    if not expired and any(task.points is None for task in tasks_all):
        return {"status": "active"}
    if expired and not any(task.attempts or task.points is not None for task in tasks_all):
        return {"status": "cancelled", "reason": "no_answers"}

    totals = {}
    for user_id, tasks in players.items():
        points = sum(task.points or 0 for task in tasks)
        elapsed_ms = sum(
            max_task_ms if task.points is None else min(task.elapsed_ms, max_task_ms)
            for task in tasks
        )
        totals[user_id] = {"points": points, "seconds": elapsed_ms // 1000}

    first, second = players
    a, b = totals[first], totals[second]
    if a["points"] != b["points"]:
        winner = first if a["points"] > b["points"] else second
        decided_by = "points"
    elif a["seconds"] != b["seconds"]:
        winner = first if a["seconds"] < b["seconds"] else second
        decided_by = "time"
    else:
        winner = None
        decided_by = "draw"
    return {
        "status": "finished",
        "reason": "timeout" if expired and any(t.points is None for t in tasks_all) else "completed",
        "players": totals,
        "winner_id": winner,
        "decided_by": decided_by,
    }

def season_at(now):
    """Границы сезона в UTC. Год зимы определяется по январю."""
    now = now.astimezone(timezone.utc)
    year = now.year + (now.month == 12)
    index = now.month % 12 // 3
    start_month = (12, 3, 6, 9)[index]
    start = datetime(year - (index == 0), start_month, 1, tzinfo=timezone.utc)
    end = datetime(year, (3, 6, 9, 12)[index], 1, tzinfo=timezone.utc)
    return f"{('Зима', 'Весна', 'Лето', 'Осень')[index]} {year}", start, end


def season_ratings(matches):
    """Матчи передаются в порядке завершения, при равенстве времени — по ID."""
    players = {}
    for first, first_nick, second, second_nick, winner in matches:
        for uid, nick in ((first, first_nick), (second, second_nick)):
            player = players.setdefault(uid, {"rating": 1500, "played": 0, "wins": 0})
            player.update(nickname=nick, played=player["played"] + 1)
            player["wins"] += uid == winner
        a, b = players[first], players[second]
        expected = 1 / (1 + 10 ** ((b["rating"] - a["rating"]) / 400))
        score = 0.5 if winner is None else int(winner == first)
        delta = floor(20 * (score - expected) + 0.5)
        a["rating"] += delta
        b["rating"] -= delta
    return players
