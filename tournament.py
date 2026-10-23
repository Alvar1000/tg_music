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

Группы дня выбираются не наугад, а по ротации (см. _make_draw): кого не было
в прошлой сетке, тот попадает в новую, остальные места достаются тем, кто
реже играл последние две недели. Иначе за неделю у одной группы 7 появлений,
у другой 2 — и таблица очков меряет везение жеребьёвки, а не симпатии
игроков. Сетка считается один раз в день и хранится в БД (tournament_draws),
поэтому правка tournament_bands.json действует со следующего дня и не ломает
уже начатые прогоны.

Первый сезон закрыт (SEASON_CLOSED): очки заморожены, итоговая таблица с
отметкой вылетевших лежит в истории турниров (history.py). Мини-аппа сейчас
играет квалификацию 80-х — это отдельный модуль quals.py с рейтингом (очки
за каждый матч сетки, могут и уходить в минус) вместо очков за стадию.
"""
import asyncio
import json
import logging
import random
from datetime import date, timedelta

from aiohttp import web

import config
import webapp_auth
from database import db

logger = logging.getLogger(__name__)

WEBAPP_DIR = config.BASE_DIR / "webapp" / "tournament"
BANDS_PER_DAY = 16
QUIZ_NAME = "tournament"
# Окно истории для ротации: «кто реже играл» считаем за последние две недели.
HISTORY_DAYS = 14

# Первый сезон закрыт: /complete больше не начисляет очки, а итоговая таблица
# при первом старте после деплоя этого флага снимается в историю турниров
# (history.py, tournament_archive) и дальше читается оттуда. Пока идёт
# квалификация 80-х (quals.py), турнир на паузе; второй сезон — отдельная
# доработка. До снимка таблица собирается из content/tournament_bands.json —
# после него файл можно менять под второй сезон.
SEASON_CLOSED = True
# Сколько групп с конца итоговой таблицы покидают турнир.
SEASON_ELIMINATED = 10
# Ключ первого сезона в tournament_archive — хранимая строка, не переименовывать.
SEASON_ARCHIVE_KEY = "season1"
SEASON_TITLE = "Сезон 1 · 60–70-е"

# Гейт «одно зачётное прохождение в день» сам по себе неатомарен: между
# проверкой get_quiz_result_today и записью результата два одновременных
# запроса от одного пользователя проходят проверку оба, и очки начисляются
# дважды. Для публичной таблицы это прямой вектор накрутки — отправить пачку
# запросов разом и поднять любимую группу, — а смысл таблицы в том, что это
# срез по всем игрокам, а не по одному активному. Процесс с ботом ровно один
# (см. CLAUDE.md, single-instance rule на BOT_TOKEN), поэтому
# внутрипроцессного лока на критическую секцию достаточно.
_complete_lock = asyncio.Lock()


async def _today_draw() -> list[dict]:
    """Сегодняшние 16 групп в порядке пар: (draw[0], draw[1]), (draw[2], draw[3])...

    Первый запрос дня считает сетку (_make_draw) и сохраняет её в БД, все
    следующие — включая проверку /complete — читают сохранённую.

    Пустой список, если групп в content/tournament_bands.json меньше
    BANDS_PER_DAY — раунд 1/8 иначе не собрать.
    """
    pool = config.load_content("tournament_bands.json", default=[])
    today = webapp_auth.today_iso()
    keys = await db.get_tournament_draw(today)
    if keys is None:
        keys = await _make_draw(pool, today)
        if not keys:
            return []
        await db.save_tournament_draw(today, keys)
        # Два одновременных первых запроса дня могли посчитать сетку оба —
        # INSERT OR IGNORE оставил одну, её и отдаём.
        keys = await db.get_tournament_draw(today)
    # Группу могли убрать из файла уже после того, как сетка дня сложилась, —
    # она доигрывает этот день под своим ключом, без фото.
    by_key = {b["key"]: b for b in pool}
    return [by_key.get(k, {"key": k, "display": k}) for k in keys]


async def _make_draw(pool: list[dict], today: str) -> list[str]:
    """Ключи 16 групп на сегодня, уже в порядке пар.

    Приоритет при отборе: 1) дольше всех не играла (не было в окне истории —
    первой); 2) реже всех играла за последние HISTORY_DAYS дней; 3) случайно.
    Первое правило даёт гарантию «не было вчера — будет сегодня» (пока групп
    не больше 32, иначе вчерашние пропустившие просто не влезут в 16 мест),
    второе выравнивает, сколько раз кто выпадает. Пары потом тасуются
    случайно — чтобы соперники каждый день были новые.
    """
    if not await db.has_tournament_draws():
        await _backfill_legacy_draws(pool, today)
        legacy = await db.get_tournament_draw(today)
        if legacy:
            return legacy

    keys = [b["key"] for b in pool]
    if len(keys) < BANDS_PER_DAY:
        return []

    day = date.fromisoformat(today)
    history = await db.get_tournament_draws_between(
        (day - timedelta(days=HISTORY_DAYS)).isoformat(),
        (day - timedelta(days=1)).isoformat(),
    )
    return pick_rotation(keys, history, today)


def pick_rotation(
    keys: list[str], history: list[tuple[str, list[str]]], today: str,
    linked: list[list[str]] | None = None,
) -> list[str]:
    """Сама ротация (см. _make_draw) — чистая функция от пула и истории сеток
    (по возрастанию даты), чтобы квалификация (quals.py) брала ту же самую.

    linked — группы ключей, которые попадают в сетку только все вместе или не
    попадают вовсе (Metallica и Megadeth в квалификации: соперники должны
    играть в одни и те же дни). Такая связка идёт в отбор одним целым с
    приоритетом самого «засидевшегося» участника; если на неё не хватает мест,
    сетку добирают следующие, а связка стоит первой завтра.
    """
    keys = list(keys)
    last_played: dict[str, str] = {}
    times_played = dict.fromkeys(keys, 0)
    for play_date, day_keys in history:  # по возрастанию даты
        for k in day_keys:
            last_played[k] = play_date
            if k in times_played:
                times_played[k] += 1

    in_pool = set(keys)
    units: list[list[str]] = []
    taken: set[str] = set()
    for group in linked or []:
        members = [k for k in dict.fromkeys(group) if k in in_pool and k not in taken]
        if len(members) > 1:
            units.append(members)
            taken.update(members)
    units += [[k] for k in keys if k not in taken]

    rng = random.Random(today)
    rng.shuffle(units)  # порядок среди равных — случайный (сортировка ниже стабильная)
    units.sort(key=lambda u: min((last_played.get(k, ""), times_played[k]) for k in u))
    chosen: list[str] = []
    for unit in units:
        if len(chosen) + len(unit) <= BANDS_PER_DAY:
            chosen += unit
    rng.shuffle(chosen)
    return chosen


async def _backfill_legacy_draws(pool: list[dict], today: str) -> None:
    """Разово, при первом запуске ротации: восстанавливает сетки, которые
    показывались до неё, — чтобы ротация сразу знала, кто сколько играл.

    Старая формула — random.Random(дата).sample(весь список, 16), поэтому
    восстановление точное, пока список в файле не переставляли. Сегодняшний
    день досевается ею же, только если турнир сегодня уже открывали: у этих
    игроков на руках старая сетка, и их прогон должен засчитаться. Если не
    открывали — сегодня сразу работает ротация.
    """
    first = await db.get_first_tournament_day()
    if first is None or len(pool) < BANDS_PER_DAY:
        return
    end = date.fromisoformat(today)
    if not await db.feature_used_on("tournament_open", today):
        end -= timedelta(days=1)
    day = date.fromisoformat(first)
    while day <= end:
        iso = day.isoformat()
        keys = [b["key"] for b in random.Random(iso).sample(pool, BANDS_PER_DAY)]
        await db.save_tournament_draw(iso, keys)
        day += timedelta(days=1)


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


def parse_bracket(body: dict, today_keys: list[str]) -> tuple[list[list[str]] | None, str | None]:
    """Раунды сетки из тела /complete: [16 групп дня, 8, 4, 2, [чемпион]] и
    None — или None и код ошибки. Общая для этого турнира и квалификации
    (quals.py): формат запроса у них один и тот же.
    """
    round16 = body.get("round16_winners")
    qf = body.get("qf_winners")
    sf = body.get("sf_winners")
    champion = body.get("champion")
    if not (
        isinstance(round16, list) and isinstance(qf, list)
        and isinstance(sf, list) and isinstance(champion, str)
    ):
        return None, "bad_shape"
    rounds = [today_keys, round16, qf, sf, [champion]]
    if not all(_round_winners_valid(rounds[i], rounds[i + 1]) for i in range(4)):
        return None, "invalid_bracket"
    return rounds, None


async def tournament_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def tournament_today(request: web.Request) -> web.Response:
    """Сегодняшние 16 групп плюс, если initData валиден, уже сыгранный
    сегодня результат (чемпион).
    """
    if SEASON_CLOSED:
        # Пустой список — старый клиент (открытый до деплоя) покажет «турнир
        # ещё не готов», новый сюда не ходит, он играет квалификацию.
        return web.json_response({"date": webapp_auth.today_iso(), "closed": True, "bands": [], "already_completed": None})

    pool = await _today_draw()
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
    if SEASON_CLOSED:
        return web.json_response({"error": "season_closed"}, status=410)

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

    today_bands = [b["key"] for b in await _today_draw()]
    if not today_bands:
        return web.json_response({"error": "no_tournament_today"}, status=400)

    rounds, error = parse_bracket(body, today_bands)
    if error:
        return web.json_response({"error": error}, status=400)
    round16, qf, sf, (champion,) = rounds[1:]

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


async def season_standings() -> list[dict]:
    """Таблица очков по группам, посчитанная заново из tournament_band_points.

    Достраивается нулями по полному пулу content/tournament_bands.json,
    иначе группы, ни разу не прошедшие дальше 1/8, просто отсутствовали бы
    в списке вместо «внизу с 0» (get_band_points_totals нули не логирует).

    После закрытия сезона у последних SEASON_ELIMINATED строк eliminated=true.
    При равных очках выше тот, у кого больше чемпионств, — иначе граница
    вылета зависела бы от порядка групп в файле.
    """
    pool = config.load_content("tournament_bands.json", default=[])
    totals = {row["band_key"]: row for row in await db.get_band_points_totals(limit=1000)}
    rows = []
    for b in pool:
        t = totals.get(b["key"])
        rows.append({
            "key": b["key"], "display": b["display"], "photo": b.get("photo"),
            "points": t["total"] if t else 0, "titles": t["titles"] if t else 0,
        })
    rows.sort(key=lambda r: (r["points"], r["titles"]), reverse=True)
    for i, row in enumerate(rows):
        row["place"] = i + 1
        row["eliminated"] = SEASON_CLOSED and i >= len(rows) - SEASON_ELIMINATED
    return rows


async def tournament_leaderboard(request: web.Request) -> web.Response:
    """Публичная таблица очков по группам — без авторизации, это контент для
    всех пользователей (в отличие от /api/dashboard/*, там владельческий токен).

    Закрытый сезон отдаётся из архива (history.py), когда снимок уже есть, —
    чтобы таблица не поплыла от правки tournament_bands.json под второй сезон.
    """
    archived = await db.get_tournament_archive(SEASON_ARCHIVE_KEY) if SEASON_CLOSED else None
    rows = archived["standings"] if archived else await season_standings()
    return web.json_response({"leaderboard": rows, "closed": SEASON_CLOSED})


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
