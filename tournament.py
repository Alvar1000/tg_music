"""aiohttp-роуты мини-игры «Турнир групп» (Telegram Mini App).

Третий self-contained веб-модуль в дополнение к рокл-игре (server.py) и
дашборду (dashboard.py) — register_routes() добавляет роуты на тот же
web.Application из server.py:create_app(), не поднимает второй сервер (см.
server.py — почему нельзя). Игровая логика (все 15 сравнений бракета) —
на клиенте; сервер только отдаёт сегодняшнюю выборку из 16 групп и
принимает финальный результат, тот же принцип, что у «Найди группу».

Очки группам начисляются по стадии, до которой группа дошла в конкретном
прогоне (не накопительно): 1/4 = 1, 1/2 = 2, финал = 3, чемпион = 5. За
вылет в 1/8 — 0 баллов, не логируется. `/api/tournament/leaderboard`
публичный (без initData/токена) — это контент для всех пользователей, не
для владельца, в отличие от /api/dashboard/*.
"""
import asyncio
import json
import logging
import random

from aiohttp import web

import config
import webapp_auth
from database import db

logger = logging.getLogger(__name__)

WEBAPP_DIR = config.BASE_DIR / "webapp" / "tournament"
BANDS_PER_DAY = 16
QUIZ_NAME = "tournament"

# Гейт «одно зачётное прохождение в день» сам по себе неатомарен: между
# проверкой get_quiz_result_today и записью результата два одновременных
# запроса от одного пользователя проходят проверку оба, и очки начисляются
# дважды. Для публичной таблицы это прямой вектор накрутки — отправить пачку
# запросов разом и поднять любимую группу, — а смысл таблицы в том, что это
# срез по всем игрокам, а не по одному активному. Процесс с ботом ровно один
# (см. CLAUDE.md, single-instance rule на BOT_TOKEN), поэтому
# внутрипроцессного лока на критическую секцию достаточно.
_complete_lock = asyncio.Lock()


def _today_pool() -> list[dict]:
    """Сегодняшние 16 групп в порядке пар — детерминированно по дате, как у
    рокла (random.Random(today).sample фиксирует разом и состав, и порядок
    пар: (pool[0],pool[1]), (pool[2],pool[3])...).

    Пустой список, если групп в content/tournament_bands.json меньше
    BANDS_PER_DAY — раунд 1/8 иначе не собрать, а не падать на sample().
    """
    pool = config.load_content("tournament_bands.json", default=[])
    if len(pool) < BANDS_PER_DAY:
        return []
    return random.Random(webapp_auth.today_iso()).sample(pool, BANDS_PER_DAY)


def _round_winners_valid(prev_round: list, winners: list) -> bool:
    """Проверяет, что winners — реально победители пар prev_round по порядку,
    а не произвольное подмножество. Без этого подделанный запрос мог бы
    объявить чемпионом группу, проигравшую в 1/8, или вообще не
    участвовавшую сегодня — а не просто ключи не из пула.

    Один и тот же хелпер применяется 4 раза: сегодняшние 16 → round16,
    round16 → qf, qf → sf, sf → [champion] (финал — тот же вызов со
    списком из одного элемента, отдельного случая не нужно).
    """
    if len(prev_round) != len(winners) * 2:
        return False
    return all(w in (prev_round[2 * i], prev_round[2 * i + 1]) for i, w in enumerate(winners))


async def tournament_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def tournament_today(request: web.Request) -> web.Response:
    """Сегодняшние 16 групп плюс, если initData валиден, уже сыгранный
    сегодня результат (чемпион).
    """
    pool = _today_pool()
    bands = [{"key": b["key"], "display": b["display"], "photo": b.get("photo")} for b in pool]

    already_completed = None
    pairs = webapp_auth.validate_init_data(request.query.get("initData", ""), config.BOT_TOKEN)
    if pairs:
        user_id = webapp_auth.extract_user_id(pairs)
        if user_id is not None:
            already_completed = await db.get_quiz_result_today(user_id, QUIZ_NAME)
            # Клиент запрашивает этот эндпоинт ровно раз при открытии мини-аппы —
            # удобная точка учёта «заходов», тот же приём, что rockle_open.
            await db.log_feature(user_id, "tournament_open")

    return web.json_response({
        "date": webapp_auth.today_iso(),
        "bands": bands,
        "already_completed": already_completed,
    })


async def tournament_complete(request: web.Request) -> web.Response:
    """Принимает результат бракета, проверяет целостность и начисляет очки.

    Личный результат (чемпион) сохраняется через save_quiz_result — бесплатно
    попадает в /stats и на дашборд (quiz:tournament), без нового кода там.

    Гейт «одно прохождение в день»: если сегодня уже есть результат — 409 с
    уже записанным чемпионом, баллы повторно не начисляются. Переиграть
    можно (клиент это не блокирует), просто повторный прогон не влияет на
    очки — это ожидаемое поведение, не ошибка.
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

    today_bands = [b["key"] for b in _today_pool()]
    if not today_bands:
        return web.json_response({"error": "no_tournament_today"}, status=400)

    round16 = body.get("round16_winners")
    qf = body.get("qf_winners")
    sf = body.get("sf_winners")
    champion = body.get("champion")
    if not (
        isinstance(round16, list) and isinstance(qf, list)
        and isinstance(sf, list) and isinstance(champion, str)
    ):
        return web.json_response({"error": "bad_shape"}, status=400)

    if not (
        _round_winners_valid(today_bands, round16)
        and _round_winners_valid(round16, qf)
        and _round_winners_valid(qf, sf)
        and _round_winners_valid(sf, [champion])
    ):
        return web.json_response({"error": "invalid_bracket"}, status=400)

    qf_losers = [k for k in round16 if k not in qf]      # 4 × 1 балл (дошли до 1/4)
    sf_losers = [k for k in qf if k not in sf]            # 2 × 2 балла (дошли до 1/2)
    final_loser = [k for k in sf if k != champion]        # 1 × 3 балла (дошёл до финала)
    awards = (
        [(k, 1) for k in qf_losers]
        + [(k, 2) for k in sf_losers]
        + [(k, 3) for k in final_loser]
        + [(champion, 5)]
    )

    # Проверка и запись — под общим локом, иначе параллельные запросы
    # начислят очки дважды (см. _complete_lock).
    async with _complete_lock:
        existing = await db.get_quiz_result_today(user_id, QUIZ_NAME)
        if existing is not None:
            return web.json_response({"error": "already_completed", "champion": existing}, status=409)
        await db.award_tournament_points(user_id, awards)
        await db.save_quiz_result(user_id, QUIZ_NAME, champion)
    return web.json_response({"ok": True, "champion": champion})


async def tournament_promo_click(request: web.Request) -> web.Response:
    """Отмечает переход по ссылке на вечеринку с экрана чемпиона.

    Отдельный роут, а не поле в /complete: промо видно и тем, кто сегодня уже
    играл, да и клик может случиться сильно позже отправки результата. Пишем в
    тот же feature_usage, что и остальные счётчики, — значит, метрика
    `feature:party_promo` сразу доступна на дашборде без нового кода там
    (dashboard.py разбирает имя метрики, см. db.get_daily_metric).

    Ответ всегда 200: это счётчик, а не действие пользователя, и падать из-за
    него на клиенте (мешая открыть саму ссылку) нечему.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False})

    pairs = webapp_auth.validate_init_data(str(body.get("initData", "")), config.BOT_TOKEN)
    if not pairs:
        return web.json_response({"ok": False})
    user_id = webapp_auth.extract_user_id(pairs)
    if user_id is None:
        return web.json_response({"ok": False})

    await db.log_feature(user_id, "party_promo")
    return web.json_response({"ok": True})


async def tournament_leaderboard(request: web.Request) -> web.Response:
    """Публичная таблица очков по группам — без авторизации, это контент для
    всех пользователей (в отличие от /api/dashboard/*, там владельческий токен).

    Достраивается нулями по полному пулу content/tournament_bands.json,
    иначе группы, ни разу не прошедшие дальше 1/8, просто отсутствовали бы
    в списке вместо «внизу с 0» (get_band_points_totals нули не логирует).
    """
    pool = config.load_content("tournament_bands.json", default=[])
    totals = {row["band_key"]: row["total"] for row in await db.get_band_points_totals(limit=1000)}
    rows = [
        {"key": b["key"], "display": b["display"], "photo": b.get("photo"), "points": totals.get(b["key"], 0)}
        for b in pool
    ]
    rows.sort(key=lambda r: r["points"], reverse=True)
    return web.json_response({"leaderboard": rows})


def register_routes(app: web.Application) -> None:
    # Владелец мог ещё не создать папку с фото групп — add_static() на
    # несуществующую директорию валит весь процесс при старте (aiohttp
    # резолвит путь с strict=True). Тот же приём, что config.seed_playlists().
    config.TOURNAMENT_COVERS_DIR.mkdir(parents=True, exist_ok=True)

    app.router.add_get("/tournament/", tournament_page)
    app.router.add_get("/tournament", tournament_page)
    app.router.add_get("/api/tournament/today", tournament_today)
    app.router.add_post("/api/tournament/complete", tournament_complete)
    app.router.add_post("/api/tournament/promo", tournament_promo_click)
    app.router.add_get("/api/tournament/leaderboard", tournament_leaderboard)
    app.router.add_static("/api/tournament/images/", config.TOURNAMENT_COVERS_DIR)
