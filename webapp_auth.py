"""Проверка подписи Telegram WebApp initData — общая для всех Mini App модулей
(server.py: «Найди группу», tournament.py: «Турнир групп»).

Вынесена в отдельный модуль, а не оставлена в server.py: server.py делает
`import dashboard` на верхнем уровне, и если бы tournament.py импортировал
эту проверку из server.py, а server.py в ответ импортировал tournament —
это был бы циклический импорт. У самой проверки initData и так нет
зависимостей ни от чего в server.py, только от стандартной библиотеки.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl

INIT_DATA_MAX_AGE = 24 * 60 * 60  # сутки — старее не принимаем (защита от replay)


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


def extract_user_id(pairs: dict) -> int | None:
    """Достаёт user.id из уже провалидированных полей initData."""
    try:
        user = json.loads(pairs.get("user", ""))
        return int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()
