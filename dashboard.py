"""aiohttp-роуты приватного дашборда аналитики для владельца бота.

Отдельный модуль от server.py (тот целиком про мини-игру для обычных
пользователей) — register_routes() добавляет роуты на уже существующий
web.Application из server.py:create_app(). Второй aiohttp-сервер заводить
нельзя (единственный процесс, единственный диск на Render — см. server.py).

Авторизация — общий секретный токен (DASHBOARD_TOKEN), не Telegram-логин:
дашборд открывается как обычный сайт в браузере. Пустой DASHBOARD_TOKEN
молча выключает все /api/dashboard/* роуты (401), как уже деградируют
WEBAPP_URL/MENU_IMAGE при отсутствии — а не роняет бота при старте.
"""
import hmac
import logging
from datetime import date, datetime, timedelta

from aiohttp import web

import config
from database import db

logger = logging.getLogger(__name__)

WEBAPP_DASHBOARD_DIR = config.BASE_DIR / "webapp" / "dashboard"
RELEASE_KINDS = {"content", "feature", "giveaway"}


def check_dashboard_auth(request: web.Request) -> bool:
    """Сверяет заголовок Authorization: Bearer <token> с DASHBOARD_TOKEN."""
    if not config.DASHBOARD_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[len("Bearer "):]
    return hmac.compare_digest(token, config.DASHBOARD_TOKEN)


def _unauthorized() -> web.Response:
    return web.json_response({"error": "unauthorized"}, status=401)


def _clamp(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _range_from_days(days: int) -> tuple[str, str]:
    """(since, until) в ISO — последние `days` суток включая сегодня (UTC)."""
    today = date.today()
    since = today - timedelta(days=days - 1)
    return since.isoformat(), today.isoformat()


def _normalize_timestamp(raw: str) -> str | None:
    """Приводит 'YYYY-MM-DDTHH:MM' (как отдаёт <input type="datetime-local">)

    или уже готовый 'YYYY-MM-DD HH:MM:SS' к единому формату проекта. None,
    если строка не распознана как дата/время.
    """
    raw = raw.strip().replace("T", " ")
    if len(raw) == 16:
        raw += ":00"
    try:
        datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return raw


async def dashboard_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DASHBOARD_DIR / "index.html")


async def login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "bad_json"}, status=400)
    token = str(body.get("token", ""))
    if not config.DASHBOARD_TOKEN or not hmac.compare_digest(token, config.DASHBOARD_TOKEN):
        return _unauthorized()
    return web.json_response({"ok": True, "token": token})


async def kpi(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    compare_days = _clamp(request.query.get("compare_days"), 7, 1, 90)
    summary = await db.get_kpi_summary(compare_days)
    mau = await db.get_mau()
    stickiness_pct = round(summary["active_today"] / mau * 100, 1) if mau > 0 else None
    return web.json_response({**summary, "mau": mau, "stickiness_pct": stickiness_pct})


async def series(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    metric = request.query.get("metric", "dau")
    days = _clamp(request.query.get("days"), 30, 1, 365)
    since, until = _range_from_days(days)
    try:
        data = await db.get_daily_metric(metric, since, until)
    except ValueError:
        return web.json_response({"error": "bad_metric"}, status=400)
    return web.json_response({"metric": metric, "series": data})


async def subscribers(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    days = _clamp(request.query.get("days"), 30, 1, 365)
    since, until = _range_from_days(days)
    data = await db.get_subscriber_growth(since, until)
    return web.json_response({"series": data})


async def subscription_flow(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    days = _clamp(request.query.get("days"), 7, 1, 90)
    data = await db.get_subscription_flow(days)
    return web.json_response(data)


async def releases_list(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    data = await db.list_releases()
    return web.json_response({"releases": data})


async def releases_create(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "bad_json"}, status=400)
    label = str(body.get("label", "")).strip()
    kind = str(body.get("kind", "content")).strip() or "content"
    released_at = _normalize_timestamp(str(body.get("released_at", "")))
    note = body.get("note") or None
    if not label or not released_at:
        return web.json_response({"error": "label_and_released_at_required"}, status=400)
    if kind not in RELEASE_KINDS:
        return web.json_response({"error": "bad_kind"}, status=400)
    release_id = await db.create_release(label, kind, released_at, note)
    return web.json_response(await db.get_release(release_id))


async def release_impact(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    try:
        release_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    release = await db.get_release(release_id)
    if release is None:
        return web.json_response({"error": "not_found"}, status=404)
    metric = request.query.get("metric", "dau")
    window_days = _clamp(request.query.get("window_days"), 7, 1, 60)
    try:
        impact = await _compute_impact(release, metric, window_days)
    except ValueError:
        return web.json_response({"error": "bad_metric"}, status=400)
    return web.json_response(impact)


async def _compute_impact(release: dict, metric: str, window_days: int) -> dict:
    """Среднее метрики за window_days суток до релиза и после.

    Окно "до" = [D-N, D-1], окно "после" = [D, D+N-1], где D — день релиза.
    Неполное окно "после" (релиз недавний) считается по факту прошедших
    дней, а не как будто данных за весь период уже накопилось.
    """
    release_day = date.fromisoformat(release["released_at"][:10])
    today = date.today()

    before_since = release_day - timedelta(days=window_days)
    before_until = release_day - timedelta(days=1)
    before_series = await db.get_daily_metric(metric, before_since.isoformat(), before_until.isoformat())

    after_until_full = release_day + timedelta(days=window_days - 1)
    after_until = min(after_until_full, today)
    days_elapsed_after = (after_until - release_day).days + 1 if after_until >= release_day else 0
    after_series = (
        await db.get_daily_metric(metric, release_day.isoformat(), after_until.isoformat())
        if days_elapsed_after > 0
        else []
    )

    def _avg(series: list[dict]) -> float:
        return sum(r["n"] for r in series) / len(series) if series else 0.0

    before_avg = _avg(before_series)
    after_avg = _avg(after_series)
    delta_pct = round((after_avg - before_avg) / before_avg * 100, 1) if before_avg > 0 else None

    return {
        "release": release,
        "metric": metric,
        "window_days": window_days,
        "before_avg": round(before_avg, 2),
        "after_avg": round(after_avg, 2),
        "days_elapsed_after": min(days_elapsed_after, window_days),
        "delta_pct": delta_pct,
    }


async def leaderboard(request: web.Request) -> web.Response:
    if not check_dashboard_auth(request):
        return _unauthorized()
    metric = request.query.get("metric", "rockle")
    limit = _clamp(request.query.get("limit"), 10, 1, 50)
    days = _clamp(request.query.get("days"), 30, 1, 365)
    since, until = _range_from_days(days)
    try:
        rows = await db.get_leaderboard(metric, since, until, limit)
    except ValueError:
        return web.json_response({"error": "bad_metric"}, status=400)
    return web.json_response({"metric": metric, "since": since, "until": until, "leaderboard": rows})


def register_routes(app: web.Application) -> None:
    app.router.add_get("/dashboard/", dashboard_page)
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_post("/api/dashboard/login", login)
    app.router.add_get("/api/dashboard/kpi", kpi)
    app.router.add_get("/api/dashboard/series", series)
    app.router.add_get("/api/dashboard/subscribers", subscribers)
    app.router.add_get("/api/dashboard/subscription-flow", subscription_flow)
    app.router.add_get("/api/dashboard/releases", releases_list)
    app.router.add_post("/api/dashboard/releases", releases_create)
    app.router.add_get("/api/dashboard/releases/{id}/impact", release_impact)
    app.router.add_get("/api/dashboard/leaderboard", leaderboard)
