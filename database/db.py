"""Слой работы с SQLite через aiosqlite.

Держим одно соединение на всё приложение: aiosqlite выполняет запросы
последовательно в отдельном потоке, поэтому это безопасно и просто.
"""
import asyncio
import json
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import aiosqlite

from duel_scoring import TaskScore, calculate_result, season_at, season_ratings
from duel_content import pick_chronology
from config import DB_PATH

logger = logging.getLogger(__name__)

# Единое соединение на весь процесс. Создаётся в init_db(), закрывается в close_db().
_db: aiosqlite.Connection | None = None


_write_lock = asyncio.Lock()


def _write(operation):
    """Одна операция владеет записью до commit или rollback на общем соединении."""
    @wraps(operation)
    async def wrapped(*args, **kwargs):
        async with _write_lock:
            try:
                result = await operation(*args, **kwargs)
                await _db.commit()
                return result
            except BaseException:
                await _db.rollback()
                raise

    return wrapped


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
    global _db, _write_lock
    _write_lock = asyncio.Lock()
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

        CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY,
            author_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('direct', 'open')),
            target_nickname TEXT,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            reply_deadline INTEGER NOT NULL,
            closed_at INTEGER,
            UNIQUE (author_id, request_id)
        );

        CREATE TABLE IF NOT EXISTS duel_players (
            duel_id INTEGER NOT NULL REFERENCES duels(id),
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('author', 'opponent')),
            nickname TEXT NOT NULL,
            released_at INTEGER,
            PRIMARY KEY (duel_id, user_id),
            UNIQUE (duel_id, role)
        );

        CREATE TABLE IF NOT EXISTS duel_tasks (
            id INTEGER PRIMARY KEY, duel_id INTEGER NOT NULL REFERENCES duels(id),
            position INTEGER NOT NULL, round TEXT NOT NULL, source_key TEXT NOT NULL, display_order TEXT,
            UNIQUE (duel_id, position)
        );
        CREATE TABLE IF NOT EXISTS duel_answers (
            task_id INTEGER NOT NULL REFERENCES duel_tasks(id), user_id INTEGER NOT NULL,
            shown_at INTEGER NOT NULL, attempts TEXT NOT NULL DEFAULT '[]',
            points INTEGER, elapsed_ms INTEGER, intro_level INTEGER NOT NULL DEFAULT 0, tale_hint INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS duel_notifications (
            duel_id INTEGER NOT NULL REFERENCES duels(id),
            event_key TEXT NOT NULL,
            recipient_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            finished_at INTEGER,
            message_id INTEGER,
            error TEXT,
            PRIMARY KEY (duel_id, event_key)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_player_busy
            ON duel_players(user_id) WHERE released_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_duel_pending_deadline
            ON duels(reply_deadline) WHERE status = 'pending';

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

        -- Рейтинговые турниры (квалификация 80-х и следующие): тот же формат
        -- сетки дня, но с колонкой tournament — у tournament_draws PK только
        -- play_date, два турнира в один день туда не лягут.
        CREATE TABLE IF NOT EXISTS rated_draws (
            tournament TEXT NOT NULL,
            play_date  TEXT NOT NULL,
            band_keys  TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tournament, play_date)
        );

        -- Каждый матч сетки (15 за засчитанный прогон). Рейтинг не хранится —
        -- он считается из матчей, поэтому формулу можно поменять задним числом.
        CREATE TABLE IF NOT EXISTS rated_matches (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament TEXT NOT NULL,
            user_id    INTEGER,
            play_date  TEXT NOT NULL,
            round      INTEGER NOT NULL,
            winner     TEXT NOT NULL,
            loser      TEXT NOT NULL,
            played_at  TEXT NOT NULL
        );

        -- История турниров: итоговая таблица завершённого турнира, снятая один
        -- раз (history.py). Живые таблицы считаются из очков/матчей и состава в
        -- content/*.json и поплыли бы от следующего сезона — архив нет.
        CREATE TABLE IF NOT EXISTS tournament_archive (
            tournament  TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            kind        TEXT NOT NULL,
            start_date  TEXT,
            end_date    TEXT,
            runs        INTEGER NOT NULL,
            players     INTEGER NOT NULL,
            standings   TEXT NOT NULL,
            archived_at TEXT NOT NULL
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
        CREATE INDEX IF NOT EXISTS idx_rated_matches_tournament       ON rated_matches(tournament, id);
        """
    )
    async with _db.execute("PRAGMA table_info(duels)") as cur:
        columns = {r["name"] for r in await cur.fetchall()}
    for name, kind in (("accepted_at", "INTEGER"), ("play_deadline", "INTEGER"), ("result_json", "TEXT")):
        if name not in columns:
            await _db.execute(f"ALTER TABLE duels ADD COLUMN {name} {kind}")
    async with _db.execute("PRAGMA table_info(duel_tasks)") as cur:
        if "display_order" not in {r["name"] for r in await cur.fetchall()}:
            await _db.execute("ALTER TABLE duel_tasks ADD COLUMN display_order TEXT")
    async with _db.execute("PRAGMA table_info(duel_answers)") as cur:
        columns = {r["name"] for r in await cur.fetchall()}
    if "tale_hint" not in columns:
        await _db.execute("ALTER TABLE duel_answers ADD COLUMN tale_hint INTEGER NOT NULL DEFAULT 0")
    if "intro_level" not in columns:
        await _db.execute("ALTER TABLE duel_answers ADD COLUMN intro_level INTEGER NOT NULL DEFAULT 0")
        if "intro_seconds" in columns:
            await _db.execute("UPDATE duel_answers SET intro_level = CASE intro_seconds "
                              "WHEN 5 THEN 1 WHEN 10 THEN 2 ELSE 0 END")
    await _db.commit()
    await _backfill_subscription_events()
    await recover_duel_invitations()
    await expire_duel_challenges()
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


@_write
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


@_write
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


async def get_seen_fact_ids(user_id: int) -> set[int]:
    """Множество id фактов, которые пользователь уже видел."""
    async with _db.execute(
        "SELECT fact_id FROM seen_facts WHERE user_id = ?", (user_id,)
    ) as cur:
        rows = await cur.fetchall()
    return {row["fact_id"] for row in rows}


@_write
async def mark_fact_seen(user_id: int, fact_id: int) -> None:
    """Отмечает факт как просмотренный (повтор игнорируется)."""
    await _db.execute(
        "INSERT OR IGNORE INTO seen_facts (user_id, fact_id, seen_at) VALUES (?, ?, ?)",
        (user_id, fact_id, _now()),
    )


@_write
async def reset_seen_facts(user_id: int) -> None:
    """Сбрасывает историю просмотренных фактов пользователя."""
    await _db.execute("DELETE FROM seen_facts WHERE user_id = ?", (user_id,))


@_write
async def save_quiz_result(user_id: int, quiz_name: str, result: str) -> None:
    """Сохраняет результат теста/квеста (для аналитики)."""
    await _db.execute(
        "INSERT INTO quiz_results (user_id, quiz_name, result, completed_at) VALUES (?, ?, ?, ?)",
        (user_id, quiz_name, result, _now()),
    )


@_write
async def mark_ending_seen(user_id: int, ending_id: str) -> None:
    """Отмечает концовку квеста как открытую (повтор игнорируется)."""
    await _db.execute(
        "INSERT OR IGNORE INTO seen_endings (user_id, ending_id, seen_at) VALUES (?, ?, ?)",
        (user_id, ending_id, _now()),
    )


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


@_write
async def log_feature(user_id: int, feature: str) -> None:
    """Отмечает разовое использование фичи (для статистики «чем пользовались»)."""
    await _db.execute(
        "INSERT INTO feature_usage (user_id, feature, used_at) VALUES (?, ?, ?)",
        (user_id, feature, _now()),
    )


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


@_write
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


async def get_rockle_result(user_id: int, play_date: str) -> int | None:
    """Время (в секундах) уже сохранённого прохождения «Найди группу» за play_date, если есть."""
    async with _db.execute(
        "SELECT seconds FROM rockle_results WHERE user_id = ? AND play_date = ?",
        (user_id, play_date),
    ) as cur:
        row = await cur.fetchone()
    return row["seconds"] if row else None


@_write
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
    existing = await get_rockle_result(user_id, play_date)
    return existing if existing is not None else seconds


# ============ Релизы (для дашборда: отметки на графике трафика) ============

@_write
async def create_release(label: str, kind: str, released_at: str, note: str | None = None) -> int:
    """Создаёт отметку релиза (контент/фича/розыгрыш), возвращает id."""
    cur = await _db.execute(
        "INSERT INTO releases (label, kind, released_at, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (label, kind, released_at, note, _now()),
    )
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

@_write
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


async def get_band_points_totals(limit: int = 100) -> list[dict]:
    """Сумма очков по группам, отсортировано по убыванию; titles — сколько раз
    группа становилась чемпионом прогона (5 баллов), тай-брейк при равных очках.

    Группа, ни разу не прошедшая дальше 1/8, тут не появится (нулевые очки
    не логируются) — достраивание нулями по полному пулу групп делает
    вызывающая сторона (tournament.py), не здесь.
    """
    async with _db.execute(
        "SELECT band_key, SUM(points) AS total, SUM(points = 5) AS titles FROM tournament_band_points "
        "GROUP BY band_key ORDER BY total DESC, titles DESC LIMIT ?",
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


@_write
async def save_tournament_draw(play_date: str, band_keys: list[str]) -> None:
    """Фиксирует сетку дня. INSERT OR IGNORE: если два первых запроса дня
    посчитали сетку одновременно, остаётся одна запись — её и читают все.
    """
    await _db.execute(
        "INSERT OR IGNORE INTO tournament_draws (play_date, band_keys, created_at) VALUES (?, ?, ?)",
        (play_date, json.dumps(band_keys), _now()),
    )


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


# ============ Рейтинговые турниры (квалификация 80-х, см. quals.py) ============

async def get_rated_draw(tournament: str, play_date: str) -> list[str] | None:
    """Сетка рейтингового турнира за play_date — ключи в порядке пар, — или None."""
    async with _db.execute(
        "SELECT band_keys FROM rated_draws WHERE tournament = ? AND play_date = ?",
        (tournament, play_date),
    ) as cur:
        row = await cur.fetchone()
    return json.loads(row["band_keys"]) if row else None


@_write
async def save_rated_draw(tournament: str, play_date: str, band_keys: list[str]) -> None:
    """INSERT OR IGNORE — как save_tournament_draw: из двух одновременных
    первых запросов дня остаётся одна сетка."""
    await _db.execute(
        "INSERT OR IGNORE INTO rated_draws (tournament, play_date, band_keys, created_at) VALUES (?, ?, ?, ?)",
        (tournament, play_date, json.dumps(band_keys), _now()),
    )


async def get_rated_draws_between(tournament: str, since: str, until: str) -> list[tuple[str, list[str]]]:
    """Сетки за [since, until] по возрастанию даты — история для ротации."""
    async with _db.execute(
        "SELECT play_date, band_keys FROM rated_draws "
        "WHERE tournament = ? AND play_date BETWEEN ? AND ? ORDER BY play_date",
        (tournament, since, until),
    ) as cur:
        rows = await cur.fetchall()
    return [(row["play_date"], json.loads(row["band_keys"])) for row in rows]


@_write
async def add_rated_matches(
    tournament: str, user_id: int, play_date: str, matches: list[tuple[int, str, str]]
) -> None:
    """Записывает матчи одного прогона: (раунд, победитель, проигравший) по
    порядку игры — рейтинг потом пересчитывается в порядке id."""
    now = _now()
    await _db.executemany(
        "INSERT INTO rated_matches (tournament, user_id, play_date, round, winner, loser, played_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(tournament, user_id, play_date, rnd, winner, loser, now) for rnd, winner, loser in matches],
    )


async def get_rated_matches(tournament: str) -> list[dict]:
    """Все матчи турнира в порядке записи — вход для расчёта рейтинга."""
    async with _db.execute(
        "SELECT play_date, round, winner, loser FROM rated_matches WHERE tournament = ? ORDER BY id",
        (tournament,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


# ============ История турниров (см. history.py) ============

async def get_quiz_run_stats(quiz_name: str) -> dict:
    """Засчитанные прогоны quiz_name за всё время: сколько их, сколько разных
    людей и первый/последний день (UTC) — шапка карточки в истории турниров."""
    async with _db.execute(
        "SELECT COUNT(*) AS runs, COUNT(DISTINCT user_id) AS players, "
        "MIN(DATE(completed_at)) AS first_day, MAX(DATE(completed_at)) AS last_day "
        "FROM quiz_results WHERE quiz_name = ?",
        (quiz_name,),
    ) as cur:
        return dict(await cur.fetchone())


@_write
async def save_tournament_archive(
    tournament: str, title: str, kind: str, start_date: str | None, end_date: str | None,
    runs: int, players: int, standings: list[dict],
) -> None:
    """Снимок итогов. INSERT OR IGNORE: итог пишется один раз, повторный снимок
    (перезапуск, параллельный запрос) первый не перезаписывает."""
    await _db.execute(
        "INSERT OR IGNORE INTO tournament_archive "
        "(tournament, title, kind, start_date, end_date, runs, players, standings, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tournament, title, kind, start_date, end_date, runs, players,
         json.dumps(standings, ensure_ascii=False), _now()),
    )


def _archive_row(row) -> dict:
    out = dict(row)
    out["standings"] = json.loads(out["standings"])
    return out


async def get_tournament_archive(tournament: str) -> dict | None:
    async with _db.execute("SELECT * FROM tournament_archive WHERE tournament = ?", (tournament,)) as cur:
        row = await cur.fetchone()
    return _archive_row(row) if row else None


async def get_tournament_archives() -> list[dict]:
    """Все завершённые турниры, свежие первыми."""
    async with _db.execute(
        "SELECT * FROM tournament_archive ORDER BY end_date DESC, archived_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_archive_row(row) for row in rows]


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


@_write
async def backup_database(dest: str) -> None:
    """Целостная онлайн-копия БД в файл dest (через VACUUM INTO).

    Работает на живой базе и собирает единый файл без WAL-хвостов. Файл dest
    не должен существовать заранее — SQLite создаёт его сам.
    """
    await _db.execute("VACUUM INTO ?", (dest,))


# ============ Рок-дуэль: приглашения ============

async def _expire_pending(now: int) -> int:
    """Освобождает места до смены статуса в той же транзакции. Время — UTC, мс."""
    async with _db.execute("SELECT id FROM duels WHERE status='active' AND play_deadline <= ?", (now,)) as cur:
        overdue = await cur.fetchall()
    for row in overdue:
        await _settle_game(row["id"], now, expired=True)
    await _db.execute(
        "UPDATE duel_players SET released_at = ? WHERE released_at IS NULL AND duel_id IN "
        "(SELECT id FROM duels WHERE status = 'pending' AND reply_deadline <= ?)",
        (now, now),
    )
    async with _db.execute(
        "UPDATE duels SET status = 'expired', closed_at = ? "
        "WHERE status = 'pending' AND reply_deadline <= ?", (now, now),
    ) as cur:
        return cur.rowcount


@_write
async def expire_duel_challenges() -> int:
    """Закрывает приглашения, оставшиеся без ответа, в том числе после перезапуска."""
    return await _expire_pending(time.time_ns() // 1_000_000)


@_write
async def create_duel_challenge(
    author_id: int, request_id: str, *, opponent_nickname: str | None = None,
    verified_recipient: int | None = None,
) -> dict:
    """Резервирует место автора, а для вызова по нику — также место соперника.

    Вызывающий сервис проверяет личность и актуальность подписки до записи.
    Возвращает состояние вызова в БД. Отправку в Telegram подтверждают отдельно.
    """
    if not request_id or not request_id.strip():
        raise ValueError("invalid_request_id")
    target = None
    if opponent_nickname is not None:
        target = opponent_nickname.strip().removeprefix("@").lower()
        if not target:
            raise ValueError("invalid_nickname")
    now = time.time_ns() // 1_000_000
    await _expire_pending(now)
    async with _db.execute(
        "SELECT * FROM duels WHERE author_id = ? AND request_id = ?", (author_id, request_id),
    ) as cur:
        previous = await cur.fetchone()
    if previous:
        if previous["target_nickname"] != target:
            raise ValueError("request_conflict")
        return {**dict(previous), "send_invite": False}

    async with _db.execute("SELECT * FROM users WHERE user_id = ?", (author_id,)) as cur:
        author = await cur.fetchone()
    if author is None or not author["username"] or not author["is_subscribed"]:
        raise ValueError("author_unavailable")
    players = [(author_id, "author", author["username"])]
    if target is not None:
        async with _db.execute(
            "SELECT user_id, username, is_subscribed FROM users WHERE username = ? COLLATE NOCASE",
            (target,),
        ) as cur:
            candidates = await cur.fetchall()
        if len(candidates) != 1 or not candidates[0]["is_subscribed"]:
            raise ValueError("opponent_unavailable")
        opponent = candidates[0]
        if verified_recipient is not None and opponent["user_id"] != verified_recipient:
            raise ValueError("opponent_unavailable")
        if opponent["user_id"] == author_id:
            raise ValueError("self_challenge")
        players.append((opponent["user_id"], "opponent", opponent["username"]))

    async with _db.execute(
        "INSERT INTO duels(author_id, request_id, mode, target_nickname, status, created_at, "
        "reply_deadline) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (author_id, request_id, "direct" if target else "open", target, now, now + 86_400_000),
    ) as cur:
        duel_id = cur.lastrowid
    for user_id, role, nickname in players:
        async with _db.execute(
            "INSERT INTO duel_players(duel_id, user_id, role, nickname) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) WHERE released_at IS NULL DO NOTHING",
            (duel_id, user_id, role, nickname),
        ) as cur:
            if cur.rowcount == 0:
                raise ValueError("player_busy")
    if verified_recipient is not None:
        if target is None:
            raise ValueError("invalid_nickname")
        await _db.execute(
            "INSERT INTO duel_notifications(duel_id, event_key, recipient_id, state, started_at) "
            "VALUES (?, 'invite', ?, 'sending', ?)", (duel_id, verified_recipient, now),
        )
    async with _db.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)) as cur:
        return {**dict(await cur.fetchone()), "send_invite": verified_recipient is not None}


@_write
async def close_duel_challenge(duel_id: int, actor_id: int, action: str) -> str:
    """Автор отменяет приглашение, адресат отказывается. Возвращает текущий статус."""
    if action not in {"cancel", "decline"}:
        raise ValueError("invalid_action")
    required_role = "author" if action == "cancel" else "opponent"
    async with _db.execute(
        "SELECT d.status, d.reply_deadline FROM duels d JOIN duel_players p ON p.duel_id = d.id "
        "WHERE d.id = ? AND p.user_id = ? AND p.role = ?", (duel_id, actor_id, required_role),
    ) as cur:
        duel = await cur.fetchone()
    if duel is None:
        raise ValueError("duel_not_found")
    if duel["status"] != "pending":
        return duel["status"]
    now = time.time_ns() // 1_000_000
    if duel["reply_deadline"] <= now:
        await _expire_pending(now)
        return "expired"
    status = "cancelled" if action == "cancel" else "declined"
    await _db.execute("UPDATE duels SET status = ?, closed_at = ? WHERE id = ?", (status, now, duel_id))
    await _db.execute("UPDATE duel_players SET released_at = ? WHERE duel_id = ?", (now, duel_id))
    return status


async def duel_user_known(user_id: int) -> bool:
    async with _db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cur:
        return await cur.fetchone() is not None


async def duel_recipient_id(nickname: str) -> int | None:
    async with _db.execute("SELECT user_id FROM users WHERE username = ? COLLATE NOCASE LIMIT 2",
                           (nickname,)) as cur:
        rows = await cur.fetchall()
    return rows[0]["user_id"] if len(rows) == 1 else None


@_write
async def duel_invite_state(user_id: int, request_id: str | None = None) -> dict | None:
    """Текущая дуэль участника либо результат собственного запроса, в том числе после сбоя."""
    await _expire_pending(time.time_ns() // 1_000_000)
    condition = "p.released_at IS NULL" if request_id is None else "d.author_id = ? AND d.request_id = ?"
    params = (user_id,) if request_id is None else (user_id, user_id, request_id)
    async with _db.execute(
        "SELECT d.id, d.status, d.target_nickname, p.role, other.nickname AS peer, "
        "n.state AS delivery FROM duels d JOIN duel_players p ON p.duel_id = d.id "
        "LEFT JOIN duel_players other ON other.duel_id = d.id AND other.role != p.role "
        "LEFT JOIN duel_notifications n ON n.duel_id = d.id AND n.event_key = 'invite' "
        f"WHERE p.user_id = ? AND {condition}", params,
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _finish_invite(duel_id: int, message_id: int | None, error: str | None) -> None:
    now = time.time_ns() // 1_000_000
    async with _db.execute(
        "UPDATE duel_notifications SET state = ?, finished_at = ?, message_id = ?, error = ? "
        "WHERE duel_id = ? AND event_key = 'invite' AND state = 'sending'",
        ("failed" if error else "sent", now, message_id, error, duel_id),
    ) as cur:
        if not cur.rowcount or not error:
            return
    async with _db.execute(
        "UPDATE duels SET status = 'cancelled', closed_at = ? WHERE id = ? AND status = 'pending'",
        (now, duel_id),
    ) as cur:
        if cur.rowcount:
            await _db.execute("UPDATE duel_players SET released_at = ? WHERE duel_id = ?", (now, duel_id))


@_write
async def finish_duel_invite(duel_id: int, *, message_id: int | None = None, error: str | None = None):
    if (message_id is None) == (error is None):
        raise ValueError("Нужен номер сообщения или причина ошибки")
    await _finish_invite(duel_id, message_id, error)


@_write
async def recover_duel_invitations():
    await _db.execute("UPDATE duel_notifications SET state='failed',finished_at=?,error=? "
                      "WHERE event_key LIKE 'result:%' AND state='sending'",
                      (time.time_ns() // 1_000_000, "Результат отправки неизвестен"))
    async with _db.execute("SELECT duel_id FROM duel_notifications WHERE event_key = 'invite' "
                           "AND state = 'sending'") as cur:
        rows = await cur.fetchall()
    for row in rows:
        await _finish_invite(row["duel_id"], None, "Результат отправки неизвестен")


@_write
async def accept_duel(duel_id: int, opponent_id: int, rounds: dict) -> dict:
    """Только адресат может принять вызов. Набор один на двоих до конца игры."""
    now = time.time_ns() // 1_000_000
    await _expire_pending(now)
    async with _db.execute(
        "SELECT d.author_id, d.status FROM duels d JOIN duel_players p ON p.duel_id=d.id "
        "WHERE d.id=? AND p.role='opponent' AND p.user_id=?", (duel_id, opponent_id),
    ) as cur:
        duel = await cur.fetchone()
    if not duel:
        raise ValueError("duel_not_found")
    if duel["status"] != "pending":
        return {"status": duel["status"], "started": False}
    async with _db.execute(
        "SELECT t.round, t.source_key, MAX(a.shown_at) FROM duel_answers a JOIN duel_tasks t ON t.id=a.task_id "
        "WHERE a.user_id IN (?, ?) GROUP BY t.round, t.source_key",
        (duel["author_id"], opponent_id),
    ) as cur:
        seen = {(kind, key): stamp for kind, key, stamp in await cur.fetchall()}
    tasks = []
    used_artists = set()
    for kind in ("detective", "cover", "chronology", "intro", "tale"):
        if kind == "chronology":
            for source, order in pick_chronology(rounds.get(kind, {}), used_artists, seen):
                tasks.append((duel_id, len(tasks) + 1, kind, source, json.dumps(order)))
                used_artists.update(order)
            continue
        pool = rounds.get(kind, {})
        keys = list(pool)
        random.shuffle(keys)
        keys.sort(key=lambda key: seen.get((kind, key), -1))
        chosen = {}
        for key in keys:
            artist = pool[key]["artist"]
            if artist not in used_artists:
                chosen.setdefault(artist, key)
        candidates = list(chosen.values())
        count = 3 if kind == "intro" else 2
        if len(candidates) < count:
            continue
        if kind == "tale":
            # При равной давности показа у групп одинаковый шанс, сколько бы ни было баек.
            random.shuffle(candidates)
            candidates.sort(key=lambda key: seen.get((kind, key), -1))
        for item in candidates[:count]:
            tasks.append((duel_id, len(tasks) + 1, kind, item, None))
            used_artists.add(pool[item]["artist"])
    if not tasks:
        raise ValueError("content_unavailable")
    await _db.executemany(
        "INSERT INTO duel_tasks(duel_id, position, round, source_key, display_order) VALUES (?, ?, ?, ?, ?)", tasks,
    )
    await _db.execute("UPDATE duels SET status='active', accepted_at=?, play_deadline=? WHERE id=?",
                      (now, now + 86_400_000, duel_id))
    return {"status": "active", "started": True, "author_id": duel["author_id"]}


async def _score_rows(duel_id):
    async with _db.execute(
        "SELECT p.user_id, t.id AS task_id, t.round, t.position, a.shown_at, a.attempts, "
        "a.points, a.elapsed_ms FROM duel_players p JOIN duel_tasks t ON t.duel_id=p.duel_id "
        "LEFT JOIN duel_answers a ON a.task_id=t.id AND a.user_id=p.user_id "
        "WHERE p.duel_id=? ORDER BY t.position", (duel_id,),
    ) as cur:
        return await cur.fetchall()


async def _close_game(duel_id, now, result):
    await _db.execute("UPDATE duels SET status=?, closed_at=?, result_json=? WHERE id=? AND status='active'",
                      (result["status"], now, json.dumps(result), duel_id))
    await _db.execute("UPDATE duel_players SET released_at=? WHERE duel_id=?", (now, duel_id))
    await _db.execute(
        "INSERT INTO duel_notifications(duel_id,event_key,recipient_id,state,started_at) "
        "SELECT duel_id,'result:' || user_id,user_id,'pending',? FROM duel_players WHERE duel_id=?",
        (now, duel_id),
    )


async def _settle_game(duel_id, now, expired=False):
    scores = {}
    for row in await _score_rows(duel_id):
        scores.setdefault(row["user_id"], []).append(TaskScore(
            row["points"], row["elapsed_ms"] or 0, len(json.loads(row["attempts"] or "[]")),
        ))
    result = calculate_result(scores, expired=expired)
    if result["status"] != "active":
        await _close_game(duel_id, now, result)


async def _player_game(duel_id, user_id):
    async with _db.execute(
        "SELECT d.id, d.status, d.play_deadline, d.result_json, other.nickname AS peer, "
        "other.user_id AS peer_id FROM duels d JOIN duel_players me ON me.duel_id=d.id "
        "JOIN duel_players other ON other.duel_id=d.id AND other.role!=me.role "
        "WHERE d.id=? AND me.user_id=?", (duel_id, user_id),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise ValueError("duel_not_found")
    return row


async def _game_view(duel_id, user_id):
    game = await _player_game(duel_id, user_id)
    view = {"id": duel_id, "status": game["status"], "peer": game["peer"]}
    if game["result_json"]:
        result = json.loads(game["result_json"])
        view["reason"] = result["reason"]
        if result["status"] == "finished":
            view.update(own=result["players"][str(user_id)], opponent=result["players"][str(game["peer_id"])],
                        outcome="draw" if result["winner_id"] is None else
                        ("win" if result["winner_id"] == user_id else "loss"), decided_by=result["decided_by"])
    elif game["status"] == "active":
        rows = await _score_rows(duel_id)
        own = [r for r in rows if r["user_id"] == user_id]
        following = next((r for r in own if r["points"] is None), None)
        view.update(points=sum(r["points"] or 0 for r in own), count=len(own),
                    progress=[{"round": r["round"], "points": r["points"]} for r in own],
                    next_task=following["task_id"] if following else None,
                    opened=following is not None and following["shown_at"] is not None,
                    peer_done=all(r["points"] is not None for r in rows if r["user_id"] != user_id))
    return view


@_write
async def duel_game_state(duel_id, user_id):
    await _expire_pending(time.time_ns() // 1_000_000)
    return await _game_view(duel_id, user_id)


@_write
async def play_duel_task(duel_id, user_id, task_id, rounds, artists, *, answer=None, request_id=None, intro_seconds=None, hint=False):
    """Открывает задание или принимает попытку. Все переходы сохраняются до ответа клиенту."""
    now = time.time_ns() // 1_000_000
    await _expire_pending(now)
    game = await _game_view(duel_id, user_id)
    async with _db.execute(
        "SELECT t.source_key, t.round, t.position, t.display_order, a.shown_at, a.attempts, a.intro_level, a.tale_hint FROM duel_tasks t "
        "LEFT JOIN duel_answers a ON a.task_id=t.id AND a.user_id=? WHERE t.id=? AND t.duel_id=?",
        (user_id, task_id, duel_id),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise ValueError("duel_not_found")
    attempts = json.loads(row["attempts"] or "[]")
    old = next((a for a in attempts if a["id"] == request_id), None)
    if old and old["key"] != answer:
        raise ValueError("request_conflict")
    if old:
        return {"duel": game, "feedback": old["feedback"]}
    if game["status"] != "active":
        return {"duel": game}
    if game["next_task"] != task_id:
        raise ValueError("wrong_task")
    key = row["source_key"]
    kind = row["round"]
    pool = rounds.get(kind, {})
    if kind == "chronology":
        order = json.loads(row["display_order"])
        years = pool.get("years", {})
        available = all(k in artists and k in years for k in order) and len({years[k] for k in order}) == 4
    else:
        card = pool.get(key)
        available = card is not None
    if not available:
        await _close_game(duel_id, now, {"status": "cancelled", "reason": "content_unavailable"})
        return {"duel": await _game_view(duel_id, user_id)}
    level = row["intro_level"] or 0
    durations = (5, 10, 15)
    if intro_seconds is not None:
        if kind != "intro" or row["shown_at"] is None:
            raise ValueError("wrong_task")
        if type(intro_seconds) is not int or intro_seconds not in durations[1:]:
            raise ValueError("invalid_choice")
        level = max(level, durations.index(intro_seconds))
        await _db.execute("UPDATE duel_answers SET intro_level=? WHERE task_id=? AND user_id=?",
                          (level, task_id, user_id))
        return {"seconds": durations[level]}
    if kind == "intro":
        songs = sorted(c["song"] for c in pool.values())
    if hint and (kind != "tale" or row["shown_at"] is None):
        raise ValueError("wrong_task")
    if kind == "tale":
        choices = card["choices"]
        hinted = bool(hint or row["tale_hint"])
        if hinted:
            solution = card["solution"]
            others = sorted(k for k in choices.keys() - card["accepted"] if (k in artists) == (solution in artists))
            if len(others) < 3:
                raise ValueError("invalid_choice")
            rng = random.Random(task_id)
            variants = [solution, *rng.sample(others, 3)]
            rng.shuffle(variants)
            choices = {k: choices[k] for k in variants}
            if hint and not row["tale_hint"]:
                await _db.execute("UPDATE duel_answers SET tale_hint=1 WHERE task_id=? AND user_id=?", (task_id, user_id))
    if answer is None:
        await _db.execute("INSERT OR IGNORE INTO duel_answers(task_id,user_id,shown_at) VALUES (?,?,?)",
                          (task_id, user_id, now))
        if kind == "chronology":
            question = {"prompt": "Нажимай на группы по году основания — от самой старой к самой молодой."}
            options = [artists[k] for k in order]
        elif kind == "intro":
            question = {"prompt": "Какая песня звучит?", "audio": f"/duel/intros/{key}",
                        "seconds": durations[level], "songs": songs}
            options = []
        elif kind == "tale":
            question = {field: card[field] for field in ("prompt", "text", "tags")}
            question["hinted"] = hinted
            options = list(choices.values())
        else:
            question = {"prompt": card["prompt"]}
            field = "image" if kind == "cover" else "facts"
            question[field] = card[field]
            options = list(artists.values())
        return {"task": {**question, "round": kind, "id": task_id, "position": row["position"],
                         "shown_at": row["shown_at"] if row["shown_at"] is not None else now,
                         "wrong": [a["key"] for a in attempts]}, "artists": options}
    if row["shown_at"] is None:
        raise ValueError("wrong_task")
    if kind == "chronology":
        if not isinstance(answer, list) or len(answer) != 4 or not all(k in answer for k in order):
            raise ValueError("invalid_choice")
        correct = sorted(order, key=years.get)
        points = sum(a == b for a, b in zip(answer, correct))
        feedback = {"points": points, "chronology": [{"key": k, "year": years[k]} for k in correct]}
    elif kind == "intro":
        if answer not in songs:
            raise ValueError("invalid_choice")
        points = 3 - level if answer == card["song"] else 0
        feedback = {"points": points, "song": card["song"]}
    elif kind == "tale":
        if not isinstance(answer, str) or answer not in choices:
            raise ValueError("invalid_choice")
        points = (1 if hinted else 3) if answer in card["accepted"] else 0
        feedback = {"points": points, "solution": card["solution"], "reveal": card["reveal"]}
    else:
        if not isinstance(answer, str) or answer not in artists:
            raise ValueError("invalid_choice")
        if any(a["key"] == answer for a in attempts):
            raise ValueError("already_tried")
        remaining = 0 if kind == "cover" else 2 - len(attempts)
        points = 3 - len(attempts) if answer == card["artist"] else None
        if points is None and remaining == 0:
            points = 0
        feedback = {"points": points, "remaining": remaining}
        if points is not None:
            feedback["solution"] = card["artist"]
            if kind == "cover":
                feedback["album"] = card["album"]
    attempts.append({"id": request_id, "key": answer, "at": now, "feedback": feedback})
    elapsed = min(120_000, max(0, now - row["shown_at"])) if points is not None else None
    await _db.execute("UPDATE duel_answers SET attempts=?,points=?,elapsed_ms=? WHERE task_id=? AND user_id=?",
                      (json.dumps(attempts), points, elapsed, task_id, user_id))
    if points is not None:
        await _settle_game(duel_id, now)
    return {"duel": await _game_view(duel_id, user_id), "feedback": feedback}


@_write
async def claim_duel_results():
    """Одна попытка сообщения каждому игроку. Итог доступен по ссылке независимо от доставки."""
    await _expire_pending(time.time_ns() // 1_000_000)
    async with _db.execute("SELECT duel_id,recipient_id FROM duel_notifications "
                           "WHERE state='pending' AND event_key LIKE 'result:%' LIMIT 1") as cur:
        row = await cur.fetchone()
    if not row:
        return None
    duel_id, user_id = row
    await _db.execute("UPDATE duel_notifications SET state='sending',started_at=? WHERE duel_id=? AND event_key=?",
                      (time.time_ns() // 1_000_000, duel_id, f"result:{user_id}"))
    return user_id, await _game_view(duel_id, user_id)


@_write
async def finish_duel_result_message(duel_id, user_id, message_id, error):
    await _db.execute("UPDATE duel_notifications SET state=?,finished_at=?,message_id=?,error=? "
                      "WHERE duel_id=? AND event_key=?", ("failed" if error else "sent",
                      time.time_ns() // 1_000_000, message_id, error, duel_id, f"result:{user_id}"))

async def duel_rating(user_id):
    season, start, end = season_at(datetime.now(timezone.utc))
    async with _db.execute(
        "SELECT a.user_id, a.nickname, b.user_id, b.nickname, d.result_json FROM duels d "
        "JOIN duel_players a ON a.duel_id=d.id AND a.role='author' "
        "JOIN duel_players b ON b.duel_id=d.id AND b.role='opponent' "
        "WHERE d.status='finished' AND d.closed_at>=? AND d.closed_at<? ORDER BY d.closed_at, d.id",
        (int(start.timestamp()) * 1000, int(end.timestamp()) * 1000),
    ) as cur:
        players = season_ratings((a, an, b, bn, json.loads(result)["winner_id"])
                                 for a, an, b, bn, result in await cur.fetchall())
    rows = sorted((p for p in players.values() if p["played"] >= 3),
                  key=lambda p: (p["rating"], p["wins"]), reverse=True)
    return {"season": season, "me": players.get(user_id, {"rating": 1500, "played": 0}), "rows": rows}
