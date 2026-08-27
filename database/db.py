"""Слой работы с SQLite через aiosqlite.

Держим одно соединение на всё приложение: aiosqlite выполняет запросы
последовательно в отдельном потоке, поэтому это безопасно и просто.
"""
import logging
from datetime import datetime, timezone

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
        """
    )
    await _db.commit()
    logger.info("База данных готова: %s", DB_PATH)


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


async def set_subscribed(user_id: int, is_subscribed: bool) -> None:
    """Обновляет флаг подписки и время последней активности."""
    await _db.execute(
        "UPDATE users SET is_subscribed = ?, last_active = ? WHERE user_id = ?",
        (1 if is_subscribed else 0, _now(), user_id),
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

    return {
        "total": total,
        "new_today": new_today,
        "subscribed": subscribed,
        "active_today": active_today,
        "tests_today": tests_today,
        "playlist_today": playlist_today,
        "rockle_today": rockle_today,
        "rockle_opens_today": rockle_opens_today,
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

    return {
        "days": days,
        "mau": mau,
        "avg_dau": avg_dau,
        "tests_month": tests_month,
        "playlist_month": playlist_month,
        "rockle_opens_month": rockle_opens_month,
        "rockle_completed_month": rockle_completed_month,
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


async def backup_database(dest: str) -> None:
    """Целостная онлайн-копия БД в файл dest (через VACUUM INTO).

    Работает на живой базе и собирает единый файл без WAL-хвостов. Файл dest
    не должен существовать заранее — SQLite создаёт его сам.
    """
    await _db.commit()  # на всякий случай закрываем возможную транзакцию
    await _db.execute("VACUUM INTO ?", (dest,))
