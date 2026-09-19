"""aiohttp-сервер мини-игры «Найди группу» (Telegram Mini App).

Работает в том же процессе и event loop'е, что и long polling бота (см.
main.py) — намеренно: постоянный диск Render с БД и очередью плейлистов
примонтирован к одному сервису, второй процесс до тех же файлов просто не
достучится. Раздаёт статическую страницу мини-игры и два JSON-эндпоинта.
"""
import json
import logging
import random

from aiohttp import web

import config
import dashboard
import tournament
import webapp_auth
from database import db

logger = logging.getLogger(__name__)

WEBAPP_DIR = config.BASE_DIR / "webapp" / "rockle"
WORDS_PER_DAY = 10


async def rockle_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def rockle_today(request: web.Request) -> web.Response:
    """Слова на сегодня (общие для всех — детерминированная выборка по дате)
    плюс, если пользователь опознан по initData, его уже засчитанный результат.
    """
    pool = config.load_content("rockle_words.json", default=[])
    today = webapp_auth.today_iso()
    words = random.Random(today).sample(pool, min(WORDS_PER_DAY, len(pool))) if pool else []

    already_completed = None
    pairs = webapp_auth.validate_init_data(request.query.get("initData", ""), config.BOT_TOKEN)
    if pairs:
        user_id = webapp_auth.extract_user_id(pairs)
        if user_id is not None:
            already_completed = await db.get_rockle_result(user_id, today)
            # Клиент запрашивает этот эндпоинт ровно раз при открытии мини-аппы —
            # удобная точка учёта «заходов», отдельно от завершённых прохождений.
            await db.log_feature(user_id, "rockle_open")

    return web.json_response({
        "date": today,
        "words": words,
        "already_completed": already_completed,
    })


async def rockle_complete(request: web.Request) -> web.Response:
    """Засчитывает прохождение. Требует валидный initData — иначе результат
    можно было бы приписать любому чужому user_id.
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

    seconds = body.get("seconds")
    if not isinstance(seconds, int) or not (0 < seconds <= 3600):
        return web.json_response({"error": "bad_seconds"}, status=400)

    recorded = await db.save_rockle_result(user_id, webapp_auth.today_iso(), seconds)
    return web.json_response({"ok": True, "seconds": recorded})


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/rockle/", rockle_page)
    app.router.add_get("/rockle", rockle_page)
    app.router.add_get("/api/rockle/today", rockle_today)
    app.router.add_post("/api/rockle/complete", rockle_complete)
    app.router.add_get("/healthz", healthz)
    dashboard.register_routes(app)
    tournament.register_routes(app)
    return app


async def start_server() -> web.AppRunner:
    """Поднимает aiohttp на config.PORT. Возвращает раннер — его нужно cleanup() при остановке."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info("Веб-сервер мини-игры поднят на порту %s", config.PORT)
    return runner
