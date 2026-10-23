"""aiohttp-роуты квалификации 80-х — сезона мини-аппы «Турнир групп», который
идёт, пока основной турнир (tournament.py) на паузе после первого сезона.

Сетка та же, что в tournament.py: 16 групп дня по ротации, 15 сравнений на
клиенте, одна зачётная сетка в день на человека, тот же формат /complete. Итог
другой: рейтинг, который может и падать. Каждый выбор в сетке — матч двух
групп: прошедшая в следующий раунд получает WIN_POINTS, вылетевшая теряет
LOSS_POINTS, победа в финале стоит FINAL_WIN_POINTS. За сетку группа получает
−4 (вылет в 1/8), 0 (1/4), +4 (1/2), +8 (финалист) или +28 (чемпион).

В БД лежат сами матчи (rated_matches), рейтинг пересчитывается из них на каждый
запрос таблицы — несколько тысяч матчей за месяц это миллисекунды. Поэтому
очки ниже можно поменять в любой момент, и вся история пересчитается по новым,
без миграций (так уже было: сначала рейтинг считался по Эло).

Состав и даты — content/tournament_quals_80s.json:
  {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "promote": 10,
   "linked": [["metallica", "megadeth"]], "bands": [...]}
Даты включительные, по UTC (как всё в боте). Группа с "contender": false
играет вне конкурса — она уже в основном турнире: рейтинг у неё считается,
место в отборе она не занимает. По итогам `promote` лучших претендентов
переходят в основной турнир. "linked" (необязательно) — группы, которые
попадают в сетку дня только вместе, чтобы у прямых соперников были равные
условия (см. tournament.pick_rotation).
"""
import asyncio
import json
import logging
from datetime import date, timedelta

from aiohttp import web

import config
import tournament
import webapp_auth
from database import db

logger = logging.getLogger(__name__)

# Ключ турнира в rated_draws/rated_matches и quiz_name в quiz_results (по нему
# /stats и дашборд считают засчитанные сетки) — хранимая строка, не переименовывать.
TOURNAMENT = "quals_80s"
CONTENT_FILE = "tournament_quals_80s.json"
# Название в истории турниров (history.py), куда итог попадает после `end`.
TITLE = "Квалификация 80-х"

START_RATING = 1500
# Очки за матч сетки, одинаковые в любом раунде, кроме победы в финале.
WIN_POINTS = 4
LOSS_POINTS = 4
FINAL_WIN_POINTS = 16
FINAL_ROUND = 3  # раунды в rated_matches: 0 — 1/8, 1 — 1/4, 2 — 1/2, 3 — финал

# Та же причина, что у tournament._complete_lock: проверка «уже играл сегодня»
# и запись матчей должны быть атомарны, иначе пачка параллельных запросов
# засчитает сетку несколько раз.
_complete_lock = asyncio.Lock()


def load_config() -> dict:
    """Настройки квалификации из контента. Битый или отсутствующий файл не
    роняет бота — квалификация просто считается выключенной (status "off")."""
    cfg = config.load_content(CONTENT_FILE, default={})
    if not isinstance(cfg, dict):
        cfg = {}
    bands = [b for b in cfg.get("bands", []) if isinstance(b, dict) and b.get("key")]
    try:
        start = date.fromisoformat(cfg["start"]).isoformat()
        end = date.fromisoformat(cfg["end"]).isoformat()
    except (KeyError, TypeError, ValueError):
        start = end = None
    try:
        promote = int(cfg.get("promote", 10))
    except (TypeError, ValueError):
        promote = 10
    linked = [
        [k for k in group if isinstance(k, str)]
        for group in cfg.get("linked", []) if isinstance(group, list)
    ] if isinstance(cfg.get("linked"), list) else []
    return {"start": start, "end": end, "promote": promote, "linked": linked, "bands": bands}


def quals_status(q: dict, today: str) -> str:
    if not q["start"] or not q["end"] or len(q["bands"]) < tournament.BANDS_PER_DAY:
        return "off"
    if today < q["start"]:
        return "upcoming"
    if today > q["end"]:
        return "finished"
    return "active"


async def _today_draw(q: dict, today: str) -> list[dict]:
    """Сегодняшние 16 групп в порядке пар — та же ротация, что в основном
    турнире (со связками из q["linked"]), и тоже считается один раз и
    хранится (rated_draws)."""
    bands = q["bands"]
    keys = await db.get_rated_draw(TOURNAMENT, today)
    if keys is None:
        day = date.fromisoformat(today)
        history = await db.get_rated_draws_between(
            TOURNAMENT,
            (day - timedelta(days=tournament.HISTORY_DAYS)).isoformat(),
            (day - timedelta(days=1)).isoformat(),
        )
        draw = tournament.pick_rotation([b["key"] for b in bands], history, today, q["linked"])
        await db.save_rated_draw(TOURNAMENT, today, draw)
        keys = await db.get_rated_draw(TOURNAMENT, today)
    by_key = {b["key"]: b for b in bands}
    return [by_key.get(k, {"key": k, "display": k}) for k in keys]


def compute_ratings(matches: list[dict], today: str) -> tuple[dict, dict, dict, dict]:
    """Прогоняет матчи по порядку и возвращает (рейтинг сейчас, рейтинг на
    начало сегодняшнего дня UTC — для ▲/▼ за день, число сыгранных матчей,
    число чемпионств — тай-брейк при равном рейтинге).
    """
    rating: dict[str, int] = {}
    played: dict[str, int] = {}
    titles: dict[str, int] = {}
    day_start = None
    for m in matches:
        if day_start is None and m["play_date"] >= today:
            day_start = dict(rating)
        w, l = m["winner"], m["loser"]
        final = m["round"] == FINAL_ROUND
        rating[w] = rating.get(w, START_RATING) + (FINAL_WIN_POINTS if final else WIN_POINTS)
        rating[l] = rating.get(l, START_RATING) - LOSS_POINTS
        played[w] = played.get(w, 0) + 1
        played[l] = played.get(l, 0) + 1
        if final:
            titles[w] = titles.get(w, 0) + 1
    return rating, day_start if day_start is not None else dict(rating), played, titles


async def quals_today(request: web.Request) -> web.Response:
    """Статус квалификации, сегодняшние 16 групп (пока она идёт) и, если
    initData валиден, уже засчитанный сегодня чемпион."""
    q = load_config()
    today = webapp_auth.today_iso()
    status = quals_status(q, today)

    bands = []
    if status == "active":
        bands = [
            {"key": b["key"], "display": b["display"], "photo": b.get("photo")}
            for b in await _today_draw(q, today)
        ]

    already_completed = None
    pairs = webapp_auth.validate_init_data(request.query.get("initData", ""), config.BOT_TOKEN)
    if pairs:
        user_id = webapp_auth.extract_user_id(pairs)
        if user_id is not None:
            if status == "active":
                already_completed = await db.get_quiz_result_today(user_id, TOURNAMENT)
            # Открытие мини-аппы «Турнир групп» — тот же счётчик, что и до
            # квалификации, чтобы ряд открытий на дашборде не рвался.
            await db.log_feature(user_id, "tournament_open")

    return web.json_response({
        "date": today,
        "status": status,
        "start": q["start"],
        "end": q["end"],
        "promote": q["promote"],
        "bands": bands,
        "already_completed": already_completed,
    })


async def quals_complete(request: web.Request) -> web.Response:
    """Принимает сетку, проверяет её как tournament.py и записывает 15 матчей.

    Повторная сетка за день — 409 с уже записанным чемпионом, матчи не пишутся
    (переиграть можно, просто не в зачёт — как в основном турнире).
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad_json"}, status=400)

    pairs = webapp_auth.validate_init_data(str(body.get("initData", "")), config.BOT_TOKEN)
    if not pairs:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    user_id = webapp_auth.extract_user_id(pairs)
    if user_id is None:
        return web.json_response({"error": "no_user"}, status=400)

    q = load_config()
    today = webapp_auth.today_iso()
    if quals_status(q, today) != "active":
        return web.json_response({"error": "quals_closed"}, status=410)

    rounds, error = tournament.parse_bracket(body, [b["key"] for b in await _today_draw(q, today)])
    if error:
        return web.json_response({"error": error}, status=400)

    matches = []
    for rnd in range(4):
        prev, winners = rounds[rnd], rounds[rnd + 1]
        for i, winner in enumerate(winners):
            a, b = prev[2 * i], prev[2 * i + 1]
            matches.append((rnd, winner, b if winner == a else a))
    champion = rounds[4][0]

    async with _complete_lock:
        existing = await db.get_quiz_result_today(user_id, TOURNAMENT)
        if existing is not None:
            return web.json_response({"error": "already_completed", "champion": existing}, status=409)
        await db.add_rated_matches(TOURNAMENT, user_id, today, matches)
        await db.save_quiz_result(user_id, TOURNAMENT, champion)
    return web.json_response({"ok": True, "champion": champion})


async def standings(q: dict, today: str) -> list[dict]:
    """Таблица рейтинга, пересчитанная из всех матчей.

    place и qualifies — только у претендентов: место среди них и попадание в
    зону прохода (первые `promote`). Зона появляется, когда каждый претендент
    сыграл хотя бы матч: в первый день половина групп ещё не выпадала в сетку
    и стоит на стартовых 1500 — выше тех, кто сыграл и проиграл, — и
    «проходила» бы, не сыграв ни разу. С ротацией это ровно один день.
    """
    matches = await db.get_rated_matches(TOURNAMENT)
    rating, day_start, played, titles = compute_ratings(matches, today)

    rows = []
    for b in q["bands"]:
        key = b["key"]
        rows.append({
            "key": key,
            "display": b["display"],
            "photo": b.get("photo"),
            "contender": b.get("contender", True) is not False,
            "rating": rating.get(key, START_RATING),
            "delta": rating.get(key, START_RATING) - day_start.get(key, START_RATING),
            "matches": played.get(key, 0),
            "titles": titles.get(key, 0),
        })
    # Очки целые, равенство частое — выше та, у кого больше чемпионств, а не
    # та, что раньше в файле или по алфавиту.
    rows.sort(key=lambda r: (-r["rating"], -r["titles"], r["display"].lower()))

    zone_ready = all(row["matches"] > 0 for row in rows if row["contender"])
    place = 0
    for row in rows:
        row["place"] = None
        row["qualifies"] = False
        if row["contender"]:
            place += 1
            row["place"] = place
            row["qualifies"] = zone_ready and place <= q["promote"]
    return rows


async def quals_rating(request: web.Request) -> web.Response:
    """Публичная таблица рейтинга — как /api/tournament/leaderboard, без авторизации."""
    q = load_config()
    today = webapp_auth.today_iso()
    return web.json_response({
        "date": today,
        "status": quals_status(q, today),
        "start": q["start"],
        "end": q["end"],
        "promote": q["promote"],
        "bands": await standings(q, today),
    })


def register_routes(app: web.Application) -> None:
    # Страницу (/tournament/) и фото групп (/api/tournament/images/) раздаёт
    # tournament.py — мини-аппа одна, фото новых групп лежат в той же папке.
    app.router.add_get("/api/quals/today", quals_today)
    app.router.add_post("/api/quals/complete", quals_complete)
    app.router.add_get("/api/quals/rating", quals_rating)
