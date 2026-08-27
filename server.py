"""aiohttp-сервер мини-игры «Найди группу» (Telegram Mini App).

Работает в том же процессе и event loop'е, что и long polling бота (см.
main.py) — намеренно: постоянный диск Render с БД и очередью плейлистов
примонтирован к одному сервису, второй процесс до тех же файлов просто не
достучится. Раздаёт статическую страницу мини-игры и два JSON-эндпоинта.
"""
import hashlib
import hmac
import json
import logging
import random
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from aiohttp import web

import config
from database import db

logger = logging.getLogger(__name__)

WEBAPP_DIR = config.BASE_DIR / "webapp" / "rockle"
INIT_DATA_MAX_AGE = 24 * 60 * 60  # сутки — старее не принимаем (защита от replay)
WORDS_PER_DAY = 15


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверяет подпись Telegram WebApp initData, возвращает поля или None.

    Алгоритм из документации Telegram (Validating data received via the Mini App):
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if datetime.now(timezone.utc).timestamp() - auth_date > INIT_DATA_MAX_AGE:
        return None  # старый initData — не принимаем (защита от повторного использования)

    return pairs


def _extract_user_id(pairs: dict) -> int | None:
    """Достаёт user.id из уже провалидированных полей initData."""
    try:
        user = json.loads(pairs.get("user", ""))
        return int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def rockle_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def rockle_today(request: web.Request) -> web.Response:
    """Слова на сегодня (общие для всех — детерминированная выборка по дате)
    плюс, если пользователь опознан по initData, его уже засчитанный результат.
    """
    pool = config.load_content("rockle_words.json", default=[])
    today = _today()
    words = random.Random(today).sample(pool, min(WORDS_PER_DAY, len(pool))) if pool else []

    already_completed = None
    pairs = validate_init_data(request.query.get("initData", ""), config.BOT_TOKEN)
    if pairs:
        user_id = _extract_user_id(pairs)
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

    pairs = validate_init_data(str(body.get("initData", "")), config.BOT_TOKEN)
    if not pairs:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    user_id = _extract_user_id(pairs)
    if user_id is None:
        return web.json_response({"error": "no_user"}, status=400)

    seconds = body.get("seconds")
    if not isinstance(seconds, int) or not (0 < seconds <= 3600):
        return web.json_response({"error": "bad_seconds"}, status=400)

    recorded = await db.save_rockle_result(user_id, _today(), seconds)
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
