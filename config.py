"""Настройки бота и доступ к контенту.

Все секреты и ссылки читаются из .env, а тексты/тесты лежат в папке content/.
Так владелец канала может менять контент, не трогая код.
"""
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Корень проекта — папка, где лежит этот файл.
BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "content"
# Обложки альбомов для теста «Угадай группу по обложке».
COVERS_DIR = CONTENT_DIR / "covers"

# Загружаем переменные окружения из .env (если файл существует).
load_dotenv(BASE_DIR / ".env")


def _parse_channel_id(raw: str):
    """CHANNEL_ID поддерживает два формата: '@username' и числовой '-100...'.

    Telegram API принимает либо строку '@username', либо целое число chat_id,
    поэтому числовой id приводим к int, а '@username' оставляем строкой.
    """
    raw = (raw or "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _parse_admin_ids(raw: str) -> set[int]:
    """ADMIN_IDS — список id через запятую -> множество чисел."""
    ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _parse_bool(raw: str, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = _parse_channel_id(os.getenv("CHANNEL_ID", ""))
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
COMMUNITY_CHAT_URL = os.getenv("COMMUNITY_CHAT_URL", "").strip()
YANDEX_PLAYLIST_URL = os.getenv("YANDEX_PLAYLIST_URL", "").strip()
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
RESET_FACTS_WHEN_DONE = _parse_bool(os.getenv("RESET_FACTS_WHEN_DONE"), False)

# Картинка-баннер для главного меню: путь к файлу или URL. Если пусто — бот сам
# ищет content/menu.jpg (а также menu.png/menu.jpeg/menu.webp).
MENU_IMAGE = os.getenv("MENU_IMAGE", "").strip()

# Пути к базе данных и лог-файлу (можно переопределить в .env).
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "bot.db"))
LOG_FILE = os.getenv("LOG_FILE", str(BASE_DIR / "bot.log"))

# Очередь «плейлиста дня». По умолчанию — файл в content/, но путь можно вынести
# на постоянный диск через PLAYLISTS_PATH (например, /data/playlists.json на
# Render), чтобы загрузки через бота переживали передеплой: папка проекта на
# хостинге обычно эфемерная, а диск — нет.
PLAYLISTS_PATH = Path(os.getenv("PLAYLISTS_PATH", str(CONTENT_DIR / "playlists.json")))

# Публичный HTTPS-адрес этого же процесса (aiohttp-сервер из server.py), без
# слеша на конце — например https://blackmagicwoman-bot.onrender.com. Нужен,
# чтобы собрать ссылку на мини-игру для кнопки web_app. На Render подхватывается
# сам через RENDER_EXTERNAL_URL (её конкретный сервис заполняет автоматически);
# WEBAPP_URL в .env — явный оверрайд поверх неё (например, туннель ngrok при
# локальной разработке). Пусто в обоих — кнопка мини-игры в меню не показывается.
WEBAPP_URL = (
    os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
)

# Порт, на котором server.py поднимает aiohttp (Render передаёт его через PORT).
PORT = int(os.getenv("PORT", "8080"))


def validate() -> None:
    """Проверяет обязательные переменные. Вызывается один раз при старте."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен "
            "бота от @BotFather."
        )
    if not CHANNEL_ID:
        raise RuntimeError(
            "Не задан CHANNEL_ID. Укажи @username канала или числовой id (-100...) в .env."
        )


def _load_json(path, default=None):
    """Читает JSON по пути path. При ошибке логирует и возвращает default.

    Файл читается каждый раз заново — значит, правки контента подхватываются
    без перезапуска бота.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # Часть контента можно добавить позже — раздел просто покажет заглушку.
        logger.warning("Файл контента отсутствует: %s", path)
    except json.JSONDecodeError as e:
        logger.error("Ошибка разбора JSON в %s: %s", path, e)
    return default


def load_content(name: str, default=None):
    """Читает JSON из папки content/ (см. _load_json)."""
    return _load_json(CONTENT_DIR / name, default)


def load_playlists(default=None):
    """Очередь плейлистов из PLAYLISTS_PATH (по умолчанию content/playlists.json)."""
    return _load_json(PLAYLISTS_PATH, default)


def seed_playlists() -> None:
    """Засевает очередь на диск, если PLAYLISTS_PATH вынесен наружу и пуст.

    На свежем диске (например, /data на Render) файла ещё нет — копируем туда
    стартовую очередь из репозитория, иначе бот начнёт с пустого «плейлиста дня».
    Если путь не переопределён или файл уже есть — ничего не делаем.
    """
    seed = CONTENT_DIR / "playlists.json"
    if PLAYLISTS_PATH == seed or PLAYLISTS_PATH.exists() or not seed.exists():
        return
    try:
        PLAYLISTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLAYLISTS_PATH.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Очередь плейлистов засеяна на диск: %s", PLAYLISTS_PATH)
    except OSError as e:
        logger.error("Не удалось засеять плейлисты на диск: %s", e)
