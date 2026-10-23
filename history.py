"""История турниров — вкладка «История» мини-аппы «Турнир групп».

Живые таблицы пересчитываются на каждый запрос: первый сезон — из очков в
tournament_band_points и состава content/tournament_bands.json, квалификация —
из матчей и content/tournament_quals_80s.json. Итог прошлого турнира от этого
поплыл бы: новый сезон начислит очки в ту же таблицу, состав под него правится
в том же файле. Поэтому итоговая таблица завершённого турнира снимается в
tournament_archive один раз и дальше читается только оттуда.

Снимок пишется при старте бота (main.py) и на каждый запрос истории — так
квалификация попадает в архив в первый же день после `end`, без перезапуска.
Повторно не пишется (INSERT OR IGNORE): переснять итог — удалить его строку
из tournament_archive руками. Отдельный модуль, а не часть tournament.py:
quals.py импортирует tournament, обратный импорт был бы циклическим.
"""
import logging

from aiohttp import web

import quals
import tournament
import webapp_auth
from database import db

logger = logging.getLogger(__name__)


async def _archive_season1() -> None:
    if not tournament.SEASON_CLOSED or await db.get_tournament_archive(tournament.SEASON_ARCHIVE_KEY):
        return
    stats = await db.get_quiz_run_stats(tournament.QUIZ_NAME)
    if not stats["runs"]:
        return  # пустая база (локальный запуск) — фиксировать нечего
    await db.save_tournament_archive(
        tournament.SEASON_ARCHIVE_KEY, tournament.SEASON_TITLE, "points",
        stats["first_day"], stats["last_day"], stats["runs"], stats["players"],
        await tournament.season_standings(),
    )
    logger.info("Итоги первого сезона турнира сохранены в историю")


async def _archive_quals() -> None:
    q = quals.load_config()
    today = webapp_auth.today_iso()
    if quals.quals_status(q, today) != "finished" or await db.get_tournament_archive(quals.TOURNAMENT):
        return
    stats = await db.get_quiz_run_stats(quals.TOURNAMENT)
    if not stats["runs"]:
        return
    rows = await quals.standings(q, today)
    for row in rows:
        row.pop("delta", None)  # изменение за день — про живую таблицу, не про итог
    await db.save_tournament_archive(
        quals.TOURNAMENT, quals.TITLE, "rating", q["start"], q["end"],
        stats["runs"], stats["players"], rows,
    )
    logger.info("Итоги квалификации сохранены в историю")


async def archive_finished() -> None:
    """Снимает итоги турниров, которые уже закончились, а в архиве их ещё нет.

    Сбой снимка не должен ронять ни старт бота, ни вкладку — пишем в лог, а
    следующий запрос истории попробует снова.
    """
    for archive in (_archive_season1, _archive_quals):
        try:
            await archive()
        except Exception:
            logger.exception("Не получилось сохранить итоги турнира в историю")


async def tournament_history(request: web.Request) -> web.Response:
    """Публичный список завершённых турниров с итоговыми таблицами, свежие первыми."""
    await archive_finished()
    return web.json_response({"tournaments": await db.get_tournament_archives()})


def register_routes(app: web.Application) -> None:
    app.router.add_get("/api/tournament/history", tournament_history)
