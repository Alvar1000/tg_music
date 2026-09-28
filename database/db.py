"""Слой работы с SQLite через aiosqlite.

Держим одно соединение на всё приложение: aiosqlite выполняет запросы
последовательно в отдельном потоке, поэтому это безопасно и просто.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

# Единое соединение на весь процесс. Создаётся в init_db(), закрывается в close_db().
_db: aiosqlite.Connection | None = None


def _now() -> str:
    """Текущее время UTC в формате, понятном функциям SQLite (DATE и т.п.)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _mark_daily_active(user_id: int) -> None:
    """Отмечает пользователя активным сегодня (для MAU/среднего DAU за месяц).

    INSERT OR IGNORE — за день пишется не больше одной строки на пользователя,
    сколько бы действий он ни совершил.
    """
    await _db.execute(
        "INSERT OR IGNORE INTO daily_active (user_id, day) VALUES (?, DATE('now'))",
        (user_id,),
    )


async def init_db() -> None:
    """Открывает соединение и создаёт таблицы, если их ещё нет."""
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            username      TEXT,
            full_name     TEXT,
            first_seen    TEXT,
            last_active   TEXT,
            is_subscribed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS seen_facts (
            user_id INTEGER,
            fact_id INTEGER,
            seen_at TEXT,
            PRIMARY KEY (user_id, fact_id)
        );

        CREATE TABLE IF NOT EXISTS quiz_results (
            user_id      INTEGER,
            quiz_name    TEXT,
            result       TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS seen_endings (
            user_id   INTEGER,
            ending_id TEXT,
            seen_at   TEXT,
            PRIMARY KEY (user_id, ending_id)
        );

        CREATE TABLE IF NOT EXISTS playlist_state (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            current_index INTEGER NOT NULL DEFAULT 0,
            last_advance  TEXT
        );

        CREATE TABLE IF NOT EXISTS feature_usage (
            user_id INTEGER,
            feature TEXT,
            used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_active (
            user_id INTEGER,
            day     TEXT,
            PRIMARY KEY (user_id, day)
        );

        CREATE TABLE IF NOT EXISTS rockle_results (
            user_id      INTEGER,
            play_date    TEXT,
            seconds      INTEGER,
            completed_at TEXT,
            PRIMARY KEY (user_id, play_date)
        );

        CREATE TABLE IF NOT EXISTS releases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            label       TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'content',
            released_at TEXT NOT NULL,
            note        TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscription_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            is_subscribed INTEGER,
            changed_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS tournament_band_points (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            band_key   TEXT,
            points     INTEGER,
            awarded_at TEXT
        );

        CREATE TABLE IF NOT EXISTS tournament_draws (
            play_date  TEXT PRIMARY KEY,
            band_keys  TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_feature_usage_feature_used_at  ON feature_usage(feature, used_at);
        CREATE INDEX IF NOT EXISTS idx_quiz_results_name_completed_at ON quiz_results(quiz_name, completed_at);
        CREATE INDEX IF NOT EXISTS idx_users_first_seen               ON users(first_seen);
        CREATE INDEX IF NOT EXISTS idx_daily_active_day               ON daily_active(day);
        CREATE INDEX IF NOT EXISTS idx_rockle_results_play_date       ON rockle_results(play_date);
        CREATE INDEX IF NOT EXISTS idx_rockle_results_user            ON rockle_results(user_id);
        CREATE INDEX IF NOT EXISTS idx_subscription_events_changed_at ON subscription_events(changed_at);
        CREATE INDEX IF NOT EXISTS idx_subscription_events_user       ON subscription_events(user_id, id);
        CREATE INDEX IF NOT EXISTS idx_tournament_band_points_band_key ON tournament_band_points(band_key);
        """
    )
    await _db.commit()
    await _backfill_subscription_events()
    logger.info("База данных готова: %s", DB_PATH)


async def _backfill_subscription_events() -> None:
    """Разовый посев истории подписок для уже подписанных пользователей.

    До появления subscription_events флаг is_subscribed не имел истории —
    без этого шага график роста подписчиков стартовал бы с нуля, теряя всех,
    кто уже подписан. Пишет по одной стартовой записи "как есть сейчас";
    если таблица не пуста, ничего не делает (безопасно на каждом старте).
    """
    async with _db.execute("SELECT COUNT(*) AS n FROM subscription_events") as cur:
        if (await cur.fetchone())["n"] > 0:
            return
    now = _now()
    async with _db.execute(
        "SELECT user_id FROM users WHERE is_subscribed = 1"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return
    await _db.executemany(
        "INSERT INTO subscription_events (user_id, is_subscribed, changed_at) VALUES (?, 1, ?)",
        [(row["user_id"], now) for row in rows],
    )
    await _db.commit()
    logger.info("Засеяна стартовая история подписок: %s пользователей", len(rows))


async def close_db() -> None:
    """Закрывает соединение с БД (вызывается при остановке бота)."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def upsert_user(user_id: int, username: str | None, full_name: str) -> None:
    """Регистрирует пользователя или обновляет его данные и время активности."""
    now = _now()
    await _db.execute(
        """
        INSERT INTO users (user_id, username, full_name, first_seen, last_active, is_subscribed)
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username    = excluded.username,
            full_name   = excluded.full_name,
            last_active = excluded.last_active
        """,
        (user_id, username, full_name, now, now),
    )
    await _mark_daily_active(user_id)
    await _db.commit()


async def set_subscribed(
    user_id: int, is_subscribed: bool, username: str | None = None, full_name: str | None = None
) -> None:
    """Обновляет флаг подписки и время последней активности.

    Если значение реально меняется — пишет строку в subscription_events
    (для графика роста подписчиков на дашборде). set_subscribed вызывается
    почти на каждое действие пользователя (гейт подписки), поэтому пишем
    только настоящие переходы, а не каждую сверку.

    Переход пишется одним INSERT ... SELECT с условием «последнее событие
    пользователя отличается» — это атомарно. Раньше флаг читался отдельным
    запросом, и два параллельных апдейта одного пользователя (двойной тап)
    читали одно и то же старое значение — переход записывался дважды.
    Пользователь без строки в users (пишет боту, минуя /start) получает её
    здесь: раньше UPDATE его не находил, и каждое его действие добавляло
    ещё одно событие «подписался».
    """
    new_value = 1 if is_subscribed else 0
    now = _now()
    await _db.execute(
        "INSERT INTO subscription_events (user_id, is_subscribed, changed_at) "
        "SELECT ?, ?, ? WHERE COALESCE("
        " (SELECT is_subscribed FROM subscription_events WHERE user_id = ? ORDER BY id DESC LIMIT 1),"
        " (SELECT is_subscribed FROM users WHERE user_id = ?),"
        " 0) != ?",
        (user_id, new_value, now, user_id, user_id, new_value),
    )
    await _db.execute(
        """
        INSERT INTO users (user_id, username, full_name, first_seen, last_active, is_subscribed)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            is_subscribed = excluded.is_subscribed,
            last_active   = excluded.last_active
        """,
        (user_id, username, full_name, now, now, new_value),
    )
    await _mark_daily_active(user_id)
    await _db.commit()


async def get_seen_fact_ids(user_id: int) -> set[int]:
    """Множество id фактов, которые пользователь уже видел."""
    async with _db.execute(
        "SELECT fact_id FROM seen_facts WHERE user_id = ?", (user_id,)
    ) as cur:
        rows = await cur.fetchall()
    return {row["fact_id"] for row in rows}


async def mark_fact_seen(user_id: int, fact_id: int) -> None:
    """Отмечает факт как просмотренный (повтор игнорируется)."""
    await _db.execute(
        "INSERT OR IGNORE INTO seen_facts (user_id, fact_id, seen_at) VALUES (?, ?, ?)",
        (user_id, fact_id, _now()),
    )
    await _db.commit()


async def reset_seen_facts(user_id: int) -> None:
    """Сбрасывает историю просмотренных фактов пользователя."""
    await _db.execute("DELETE FROM seen_facts WHERE user_id = ?", (user_id,))
    await _db.commit()


async def save_quiz_result(user_id: int, quiz_name: str, result: str) -> None:
    """Сохраняет результат теста/квеста (для аналитики)."""
    await _db.execute(
        "INSERT INTO quiz_results (user_id, quiz_name, result, completed_at) VALUES (?, ?, ?, ?)",
        (user_id, quiz_name, result, _now()),
    )
    await _db.commit()


async def mark_ending_seen(user_id: int, ending_id: str) -> None:
    """Отмечает концовку квеста как открытую (повтор игнорируется)."""
    await _db.execute(
        "INSERT OR IGNORE INTO seen_endings (user_id, ending_id, seen_at) VALUES (?, ?, ?)",
        (user_id, ending_id, _now()),
    )
    await _db.commit()


async def count_seen_endings(user_id: int, valid_ids) -> int:
    """Сколько из ныне существующих концовок (valid_ids) открыл пользователь.

    Считаем только переданные id, чтобы старые записи об удалённых концовках
    не задирали счётчик после смены сценария.
    """
    valid_ids = list(valid_ids)
    if not valid_ids:
        return 0
    placeholders = ",".join("?" for _ in valid_ids)
    async with _db.execute(
        f"SELECT COUNT(*) AS n FROM seen_endings WHERE user_id = ? AND ending_id IN ({placeholders})",
        (user_id, *valid_ids),
    ) as cur:
        return (await cur.fetchone())["n"]


async def log_feature(user_id: int, feature: str) -> None:
    """Отмечает разовое использование фичи (для статистики «чем пользовались»)."""
    await _db.execute(
        "INSERT INTO feature_usage (user_id, feature, used_at) VALUES (?, ?, ?)",
        (user_id, feature, _now()),
    )
    await _db.commit()


async def get_all_user_ids() -> list[int]:
    """Все id пользователей, когда-либо запускавших бота (для рассылки)."""
    async with _db.execute("SELECT user_id FROM users") as cur:
        rows = await cur.fetchall()
    return [row["user_id"] for row in rows]


async def get_stats() -> dict:
    """Сводка для админа.

    Общая: всего пользователей, новых за сегодня, подписанных.
    За сегодня (UTC): сколько пользователей заходило, какими тестами пользовались
    (завершённые прохождения по видам) и сколько раз открывали «Плейлист дня».
    """
    async with _db.execute("SELECT COUNT(*) AS n FROM users") as cur:
        total = (await cur.fetchone())["n"]
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE DATE(first_seen) = DATE('now')"
    ) as cur:
        new_today = (await cur.fetchone())["n"]
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE is_subscribed = 1"
    ) as cur:
        subscribed = (await cur.fetchone())["n"]

    # Посещения сегодня — уникальные пользователи с активностью за текущие сутки.
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE DATE(last_active) = DATE('now')"
    ) as cur:
        active_today = (await cur.fetchone())["n"]

    # Какими тестами пользовались сегодня (по видам, завершённые прохождения).
    async with _db.execute(
        "SELECT quiz_name, COUNT(*) AS n FROM quiz_results "
        "WHERE DATE(completed_at) = DATE('now') GROUP BY quiz_name"
    ) as cur:
        tests_today = {row["quiz_name"]: row["n"] for row in await cur.fetchall()}

    # Сколько раз открывали «Плейлист дня» сегодня.
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM feature_usage "
        "WHERE feature = 'playlist' AND DATE(used_at) = DATE('now')"
    ) as cur:
        playlist_today = (await cur.fetchone())["n"]

    # Сколько человек прошли мини-игру «Найди группу» сегодня.
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM rockle_results WHERE play_date = DATE('now')"
    ) as cur:
        rockle_today = (await cur.fetchone())["n"]

    # Сколько раз сегодня открывали мини-игру (заходы, не только завершённые).
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM feature_usage "
        "WHERE feature = 'rockle_open' AND DATE(used_at) = DATE('now')"
    ) as cur:
        rockle_opens_today = (await cur.fetchone())["n"]

    # Переходы по ссылке на вечеринку с экрана чемпиона турнира. Считаем
    # именно клики (таблица append-only), а не уникальных людей — вопрос
    # владельца был «сколько переходов».
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM feature_usage "
        "WHERE feature = 'party_promo' AND DATE(used_at) = DATE('now')"
    ) as cur:
        promo_clicks_today = (await cur.fetchone())["n"]

    return {
        "total": total,
        "new_today": new_today,
        "subscribed": subscribed,
        "active_today": active_today,
        "tests_today": tests_today,
        "playlist_today": playlist_today,
        "rockle_today": rockle_today,
        "rockle_opens_today": rockle_opens_today,
        "promo_clicks_today": promo_clicks_today,
    }


async def get_month_stats(days: int = 30) -> dict:
    """Сводка за последние `days` суток (включая сегодня): MAU, средний DAU

    и суммы по тестам/фичам — аналог get_stats(), но за окно, а не за один день.
    """
    since = f"-{days - 1} days"

    # MAU — уникальные пользователи, заходившие хотя бы раз за период.
    async with _db.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM daily_active WHERE day >= DATE('now', ?)",
        (since,),
    ) as cur:
        mau = (await cur.fetchone())["n"]

    # Средний DAU = сумма дневных «активных» строк / длина периода (дни без
    # активности учитываются как 0, а не выпадают из расчёта).
    async with _db.execute(
        "SELECT COUNT(*) AS n FROM daily_active WHERE day >= DATE('now', ?)",
        (since,),
    ) as cur:
        active_day_rows = (await cur.fetchone())["n"]
    avg_dau = active_day_rows / days

    async with _db.execute(
        "SELECT quiz_name, COUNT(*) AS n FROM quiz_results "
        "WHERE DATE(completed_at) >= DATE('now', ?) GROUP BY quiz_name",
        (since,),
    ) as cur:
        tests_month = {row["quiz_name"]: row["n"] for row in await cur.fetchall()}

    async with _db.execute(
        "SELECT COUNT(*) AS n FROM feature_usage "
        "WHERE feature = 'playlist' AND DATE(used_at) >= DATE('now', ?)",
        (since,),
    ) as cur:
        playlist_month = (await cur.fetchone())["n"]

    async with _db.execute(
        "SELECT COUNT(*) AS n FROM feature_usage "
        "WHERE feature = 'rockle_open' AND DATE(used_at) >= DATE('now', ?)",
        (since,),
    ) as cur:
        rockle_opens_month = (await cur.fetchone())["n"]

    async with _db.execute(
        "SELECT COUNT(*) AS n FROM rockle_results WHERE play_date >= DATE('now', ?)",
        (since,),
    ) as cur:
        rockle_completed_month = (await cur.fetchone())["n"]

    async with _db.execute(
        "SELECT COUNT(*) AS n FROM feature_usage "
        "WHERE feature = 'party_promo' AND DATE(used_at) >= DATE('now', ?)",
        (since,),
    ) as cur:
        promo_clicks_month = (await cur.fetchone())["n"]

    return {
        "days": days,
        "mau": mau,
        "avg_dau": avg_dau,
        "tests_month": tests_month,
        "playlist_month": playlist_month,
        "rockle_opens_month": rockle_opens_month,
        "rockle_completed_month": rockle_completed_month,
        "promo_clicks_month": promo_clicks_month,
    }


async def get_playlist_pointer() -> tuple[int, str | None]:
    """Текущий индекс «плейлиста дня» и дата последнего продвижения (ISO).

    Если строки ещё нет (бот ни разу не показывал плейлист) — начинаем с нуля.
    """
    async with _db.execute(
        "SELECT current_index, last_advance FROM playlist_state WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    return (row["current_index"], row["last_advance"]) if row else (0, None)


async def set_playlist_pointer(index: int, last_advance: str) -> None:
    """Сохраняет указатель очереди: индекс и дату продвижения (одна строка, id=1)."""
    await _db.execute(
        """
        INSERT INTO playlist_state (id, current_index, last_advance)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            current_index = excluded.current_index,
            last_advance  = excluded.last_advance
        """,
        (index, last_advance),
    )
    await _db.commit()


async def get_rockle_result(user_id: int, play_date: str) -> int | None:
    """Время (в секундах) уже сохранённого прохождения «Найди группу» за play_date, если есть."""
    async with _db.execute(
        "SELECT seconds FROM rockle_results WHERE user_id = ? AND play_date = ?",
        (user_id, play_date),
    ) as cur:
        row = await cur.fetchone()
    return row["seconds"] if row else None


async def save_rockle_result(user_id: int, play_date: str, seconds: int) -> int:
    """Сохраняет первое прохождение дня; повторные попытки не перезаписывают время.

    Возвращает засчитанное время (может быть не тем, что в этом вызове, — если
    пользователь уже проходил сегодня, остаётся первый результат).
    """
    await _db.execute(
        "INSERT OR IGNORE INTO rockle_results (user_id, play_date, seconds, completed_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, play_date, seconds, _now()),
    )
    await _db.commit()
    existing = await get_rockle_result(user_id, play_date)
    return existing if existing is not None else seconds


# ============ Релизы (для дашборда: отметки на графике трафика) ============

async def create_release(label: str, kind: str, released_at: str, note: str | None = None) -> int:
    """Создаёт отметку релиза (контент/фича/розыгрыш), возвращает id."""
    cur = await _db.execute(
        "INSERT INTO releases (label, kind, released_at, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (label, kind, released_at, note, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def list_releases(limit: int = 100) -> list[dict]:
    async with _db.execute(
        "SELECT id, label, kind, released_at, note FROM releases ORDER BY released_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_release(release_id: int) -> dict | None:
    async with _db.execute(
        "SELECT id, label, kind, released_at, note FROM releases WHERE id = ?",
        (release_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


# ============ Топ активных (для ручного выбора победителя розыгрыша) ============

async def get_leaderboard(metric: str, since: str, until: str, limit: int = 10) -> list[dict]:
    """Рейтинг пользователей за период — не механика участия в розыгрыше, а
    просто список самых активных, из которого владелец сам вручную выбирает
    победителя.

    metric="rockle" — по числу сыгранных партий в «Найди группу» (лучшее
    время — тай-брейк). metric="active" — по числу дней активности в боте
    вообще.
    """
    if metric == "rockle":
        sql = (
            "SELECT rr.user_id, u.username, u.full_name, "
            "COUNT(*) AS plays, MIN(rr.seconds) AS best_seconds "
            "FROM rockle_results rr JOIN users u ON u.user_id = rr.user_id "
            "WHERE rr.play_date BETWEEN ? AND ? "
            "GROUP BY rr.user_id ORDER BY plays DESC, best_seconds ASC LIMIT ?"
        )
    elif metric == "active":
        sql = (
            "SELECT da.user_id, u.username, u.full_name, COUNT(*) AS active_days "
            "FROM daily_active da JOIN users u ON u.user_id = da.user_id "
            "WHERE da.day BETWEEN ? AND ? "
            "GROUP BY da.user_id ORDER BY active_days DESC LIMIT ?"
        )
    else:
        raise ValueError(f"Неизвестная метрика рейтинга: {metric}")
    async with _db.execute(sql, (since, until, limit)) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


# ============ Турнир групп (мини-игра, публичная таблица очков) ============

async def award_tournament_points(user_id: int, awards: list[tuple[str, int]]) -> None:
    """Начисляет очки группам по итогам одного прогона турнира.

    awards — список (band_key, points); обычно ровно 8 строк за прогон
    (4 группы × 1 балл за 1/4, 2 × 2 балла за 1/2, 1 × 3 балла за финал,
    1 × 5 баллов чемпиону — за вылет в 1/8 очки не начисляются вовсе).
    """
    now = _now()
    await _db.executemany(
        "INSERT INTO tournament_band_points (user_id, band_key, points, awarded_at) VALUES (?, ?, ?, ?)",
        [(user_id, band_key, points, now) for band_key, points in awards],
    )
    await _db.commit()


async def get_band_points_totals(limit: int = 100) -> list[dict]:
    """Сумма очков по группам, отсортировано по убыванию.

    Группа, ни разу не прошедшая дальше 1/8, тут не появится (нулевые очки
    не логируются) — достраивание нулями по полному пулу групп делает
    вызывающая сторона (tournament.py), не здесь.
    """
    async with _db.execute(
        "SELECT band_key, SUM(points) AS total FROM tournament_band_points "
        "GROUP BY band_key ORDER BY total DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_band_points_between(since: str, until: str, limit: int = 10) -> list[dict]:
    """Сумма очков по группам за [since, until] (ISO-даты, UTC) — для
    дашборда, где всё считается за выбранное окно. Публичный лидерборд
    турнира берёт get_band_points_totals() за всё время.
    """
    async with _db.execute(
        "SELECT band_key, SUM(points) AS total FROM tournament_band_points "
        "WHERE DATE(awarded_at) BETWEEN ? AND ? "
        "GROUP BY band_key ORDER BY total DESC LIMIT ?",
        (since, until, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_quiz_result_today(user_id: int, quiz_name: str) -> str | None:
    """Результат уже пройденного сегодня (UTC) прогона quiz_name, если есть.

    Не отдельная таблица с PK на (user_id, дата) — просто запрос к уже
    существующей quiz_results. Дженерик по quiz_name, не привязан к турниру —
    подойдёт для любой будущей daily-игры так же.
    """
    async with _db.execute(
        "SELECT result FROM quiz_results WHERE user_id = ? AND quiz_name = ? "
        "AND DATE(completed_at) = DATE('now') ORDER BY completed_at DESC LIMIT 1",
        (user_id, quiz_name),
    ) as cur:
        row = await cur.fetchone()
    return row["result"] if row else None


async def get_tournament_draw(play_date: str) -> list[str] | None:
    """Сетка турнира за play_date — ключи групп в порядке пар, — или None."""
    async with _db.execute(
        "SELECT band_keys FROM tournament_draws WHERE play_date = ?", (play_date,)
    ) as cur:
        row = await cur.fetchone()
    return json.loads(row["band_keys"]) if row else None


async def save_tournament_draw(play_date: str, band_keys: list[str]) -> None:
    """Фиксирует сетку дня. INSERT OR IGNORE: если два первых запроса дня
    посчитали сетку одновременно, остаётся одна запись — её и читают все.
    """
    await _db.execute(
        "INSERT OR IGNORE INTO tournament_draws (play_date, band_keys, created_at) VALUES (?, ?, ?)",
        (play_date, json.dumps(band_keys), _now()),
    )
    await _db.commit()


async def get_tournament_draws_between(since: str, until: str) -> list[tuple[str, list[str]]]:
    """Сетки за [since, until] по возрастанию даты — история для ротации."""
    async with _db.execute(
        "SELECT play_date, band_keys FROM tournament_draws "
        "WHERE play_date BETWEEN ? AND ? ORDER BY play_date",
        (since, until),
    ) as cur:
        rows = await cur.fetchall()
    return [(row["play_date"], json.loads(row["band_keys"])) for row in rows]


async def has_tournament_draws() -> bool:
    async with _db.execute("SELECT 1 FROM tournament_draws LIMIT 1") as cur:
        return await cur.fetchone() is not None


async def get_first_tournament_day() -> str | None:
    """Первый день (UTC), когда турнир открывали или проходили; None — ещё ни разу."""
    async with _db.execute(
        "SELECT MIN(d) AS first FROM ("
        " SELECT MIN(DATE(used_at)) AS d FROM feature_usage WHERE feature = 'tournament_open'"
        " UNION ALL"
        " SELECT MIN(DATE(completed_at)) FROM quiz_results WHERE quiz_name = 'tournament'"
        ")"
    ) as cur:
        return (await cur.fetchone())["first"]


async def feature_used_on(feature: str, day: str) -> bool:
    """Было ли хоть одно использование фичи за сутки day (UTC, 'YYYY-MM-DD')."""
    async with _db.execute(
        "SELECT 1 FROM feature_usage WHERE feature = ? AND DATE(used_at) = ? LIMIT 1",
        (feature, day),
    ) as cur:
        return await cur.fetchone() is not None


# ============ Аналитика для дашборда ============

def _daily_metric_query(metric: str) -> tuple[str, tuple]:
    """SQL и доп. параметры для одной метрики get_daily_metric().

    Каждый запрос возвращает строки (day, n), сгруппированные по дню в
    пределах [since, until] (добавляются вызывающим как последние два "?").
    Дозаполнение нулями на отсутствующие дни делает сам get_daily_metric.
    """
    if metric == "new_users":
        return (
            "SELECT DATE(first_seen) AS day, COUNT(*) AS n FROM users "
            "WHERE DATE(first_seen) BETWEEN ? AND ? GROUP BY day",
            (),
        )
    if metric == "dau":
        return (
            "SELECT day, COUNT(*) AS n FROM daily_active WHERE day BETWEEN ? AND ? GROUP BY day",
            (),
        )
    if metric == "rockle_completed":
        return (
            "SELECT play_date AS day, COUNT(*) AS n FROM rockle_results "
            "WHERE play_date BETWEEN ? AND ? GROUP BY day",
            (),
        )
    if metric == "quiz_total":
        return (
            "SELECT DATE(completed_at) AS day, COUNT(*) AS n FROM quiz_results "
            "WHERE DATE(completed_at) BETWEEN ? AND ? GROUP BY day",
            (),
        )
    if metric.startswith("feature:"):
        return (
            "SELECT DATE(used_at) AS day, COUNT(*) AS n FROM feature_usage "
            "WHERE feature = ? AND DATE(used_at) BETWEEN ? AND ? GROUP BY day",
            (metric.split(":", 1)[1],),
        )
    if metric.startswith("quiz:"):
        return (
            "SELECT DATE(completed_at) AS day, COUNT(*) AS n FROM quiz_results "
            "WHERE quiz_name = ? AND DATE(completed_at) BETWEEN ? AND ? GROUP BY day",
            (metric.split(":", 1)[1],),
        )
    raise ValueError(f"Неизвестная метрика: {metric}")


def _fill_daily(by_day: dict, since: str, until: str, value_key: str) -> list[dict]:
    """Дозаполняет разреженный словарь {day: n} нулями на каждый день
    [since, until] — отсутствующий в SQL-результате день значит "0", а не
    "пропустить".
    """
    start = date.fromisoformat(since)
    end = date.fromisoformat(until)
    result = []
    d = start
    while d <= end:
        day_str = d.isoformat()
        result.append({"day": day_str, value_key: by_day.get(day_str, 0)})
        d += timedelta(days=1)
    return result


async def get_daily_metric(metric: str, since: str, until: str) -> list[dict]:
    """Дневной ряд для графика: [{"day": "YYYY-MM-DD", "n": int}, ...] на
    каждый день [since, until] включительно, недостающие дни — нули.

    metric: "new_users" | "dau" | "rockle_completed" | "feature:<name>" |
    "quiz:<name>". Один SQL-запрос на вызов — переиспользуется и графиками,
    и расчётом «до/после» у релизов.
    """
    sql, extra_params = _daily_metric_query(metric)
    async with _db.execute(sql, (*extra_params, since, until)) as cur:
        rows = await cur.fetchall()
    by_day = {row["day"]: row["n"] for row in rows}
    return _fill_daily(by_day, since, until, "n")


# Настоящие переходы подписки. Событие, повторяющее предыдущее значение того же
# пользователя, или первое событие «отписался» (отписываться было не от чего) —
# дубли, которые раньше писал set_subscribed() (гонка двойного тапа, пользователи
# без строки в users). Из таблицы их не удаляем, а отсеиваем в запросах: t.prev —
# предыдущее значение пользователя, 0 для первого события.
_TRANSITIONS_CTE = (
    "WITH t AS ("
    " SELECT user_id, is_subscribed, changed_at,"
    " LAG(is_subscribed, 1, 0) OVER (PARTITION BY user_id ORDER BY id) AS prev"
    " FROM subscription_events"
    ") "
)


async def get_subscriber_growth(since: str, until: str) -> list[dict]:
    """Дневной бегущий итог числа подписчиков: [{"day", "subscribers"}, ...].

    Считается по subscription_events как бухгалтерская книга (+1/-1), а не
    как снимок users.is_subscribed — так получается настоящая история по
    дням, а не только текущее значение. base — сумма событий до `since`,
    дальше на каждый день прибавляется дневная дельта (0, если событий не
    было).
    """
    async with _db.execute(
        _TRANSITIONS_CTE
        + "SELECT COALESCE(SUM(CASE WHEN is_subscribed = 1 THEN 1 ELSE -1 END), 0) AS n "
        "FROM t WHERE is_subscribed != prev AND DATE(changed_at) < ?",
        (since,),
    ) as cur:
        running = (await cur.fetchone())["n"]

    async with _db.execute(
        _TRANSITIONS_CTE
        + "SELECT DATE(changed_at) AS day, "
        "SUM(CASE WHEN is_subscribed = 1 THEN 1 ELSE -1 END) AS delta "
        "FROM t WHERE is_subscribed != prev AND DATE(changed_at) BETWEEN ? AND ? GROUP BY day",
        (since, until),
    ) as cur:
        deltas = {row["day"]: row["delta"] for row in await cur.fetchall()}

    start = date.fromisoformat(since)
    end = date.fromisoformat(until)
    result = []
    d = start
    while d <= end:
        day_str = d.isoformat()
        running += deltas.get(day_str, 0)
        result.append({"day": day_str, "subscribers": running})
        d += timedelta(days=1)
    return result


async def _seed_timestamp() -> str | None:
    """Момент разового посева _backfill_subscription_events(): все стартовые
    записи вставлены с одним `now`, и это самый ранний момент в таблице.
    None, если посева не было (в самый ранний момент — событие одного
    пользователя, значит, настоящее).
    """
    async with _db.execute(
        "SELECT changed_at, COUNT(DISTINCT user_id) AS n FROM subscription_events "
        "WHERE changed_at = (SELECT MIN(changed_at) FROM subscription_events) GROUP BY changed_at"
    ) as cur:
        row = await cur.fetchone()
    return row["changed_at"] if row and row["n"] > 1 else None


async def _subscription_window(since: str, until: str, seed: str | None) -> dict:
    """Подписки/отписки за [since, until]. Стартовый посев (seed) — не
    подписки этого периода, а все, кто был подписан до начала учёта, поэтому
    не считается; на графике роста он остаётся начальным скачком.
    """
    async with _db.execute(
        _TRANSITIONS_CTE
        + "SELECT is_subscribed, COUNT(*) AS n FROM t "
        "WHERE is_subscribed != prev AND DATE(changed_at) BETWEEN ? AND ? AND changed_at IS NOT ? "
        "GROUP BY is_subscribed",
        (since, until, seed),
    ) as cur:
        by_flag = {row["is_subscribed"]: row["n"] for row in await cur.fetchall()}
    subscribed = by_flag.get(1, 0)
    unsubscribed = by_flag.get(0, 0)
    return {"subscribed": subscribed, "unsubscribed": unsubscribed, "net": subscribed - unsubscribed}


async def get_subscription_flow(days: int = 7) -> dict:
    """Подписалось/отписалось/чистый прирост за последние `days` суток плюс
    дельта к предыдущему окну такой же длины (тот же принцип до/после, что
    у релизов, без привязки к конкретной дате события).
    """
    today = date.today()
    cur_since = today - timedelta(days=days - 1)
    prev_until = cur_since - timedelta(days=1)
    prev_since = prev_until - timedelta(days=days - 1)

    seed = await _seed_timestamp()
    current = await _subscription_window(cur_since.isoformat(), today.isoformat(), seed)
    previous = await _subscription_window(prev_since.isoformat(), prev_until.isoformat(), seed)

    return {
        "days": days,
        "subscribed": current["subscribed"],
        "unsubscribed": current["unsubscribed"],
        "net": current["net"],
        "subscribed_delta": current["subscribed"] - previous["subscribed"],
        "unsubscribed_delta": current["unsubscribed"] - previous["unsubscribed"],
        "net_delta": current["net"] - previous["net"],
    }


async def get_unique_active(since: str, until: str) -> int:
    """Уникальные активные пользователи за [since, until] (ISO-даты, UTC) —
    MAU, если окно 30 дней; для дашборда окно задаёт переключатель периода.
    """
    async with _db.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM daily_active WHERE day BETWEEN ? AND ?",
        (since, until),
    ) as cur:
        return (await cur.fetchone())["n"]


async def get_kpi_summary(days: int = 30) -> dict:
    """Сводка для карточек дашборда за последние `days` суток (UTC, включая
    сегодня) плюс дельта к предыдущему окну той же длины. Средний DAU
    считается так же, как в /month: дни без активности входят как 0.
    Итоговые "всего"/"подписано" — снимки на сейчас, окна у них нет.
    """
    async with _db.execute("SELECT COUNT(*) AS n FROM users") as cur:
        total = (await cur.fetchone())["n"]
    async with _db.execute("SELECT COUNT(*) AS n FROM users WHERE is_subscribed = 1") as cur:
        subscribed = (await cur.fetchone())["n"]

    today = datetime.now(timezone.utc).date()
    cur_since = today - timedelta(days=days - 1)
    prev_until = cur_since - timedelta(days=1)
    prev_since = prev_until - timedelta(days=days - 1)

    new_users_cur = await get_daily_metric("new_users", cur_since.isoformat(), today.isoformat())
    new_users_prev = await get_daily_metric("new_users", prev_since.isoformat(), prev_until.isoformat())
    dau_cur = await get_daily_metric("dau", cur_since.isoformat(), today.isoformat())
    dau_prev = await get_daily_metric("dau", prev_since.isoformat(), prev_until.isoformat())
    unique_active = await get_unique_active(cur_since.isoformat(), today.isoformat())

    def _avg(series: list[dict]) -> float:
        return sum(r["n"] for r in series) / len(series) if series else 0.0

    def _delta_pct(cur_avg: float, prev_avg: float) -> float | None:
        return round((cur_avg - prev_avg) / prev_avg * 100, 1) if prev_avg > 0 else None

    avg_dau = _avg(dau_cur)
    return {
        "days": days,
        "total": total,
        "subscribed": subscribed,
        "avg_dau": round(avg_dau, 1),
        "unique_active": unique_active,
        # Липкость = средний DAU / уникальные за окно (при 30 днях — DAU/MAU).
        "stickiness_pct": round(avg_dau / unique_active * 100, 1) if unique_active > 0 else None,
        "new_users": sum(r["n"] for r in new_users_cur),
        "new_users_delta_pct": _delta_pct(_avg(new_users_cur), _avg(new_users_prev)),
        "dau_delta_pct": _delta_pct(avg_dau, _avg(dau_prev)),
    }


async def backup_database(dest: str) -> None:
    """Целостная онлайн-копия БД в файл dest (через VACUUM INTO).

    Работает на живой базе и собирает единый файл без WAL-хвостов. Файл dest
    не должен существовать заранее — SQLite создаёт его сам.
    """
    await _db.commit()  # на всякий случай закрываем возможную транзакцию
    await _db.execute("VACUUM INTO ?", (dest,))
