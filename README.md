# Бот канала @blackmagicwoman 🤘

Telegram-бот для рок-сообщества. Конвертирует тех, кто зашёл в бота, в подписчиков
канала (через гейт подписки) и развлекает контентом: плейлист дня, факты о роке,
тесты и квест.

## Возможности

- **Гейт подписки.** Пока пользователь не подписан на канал — функционал закрыт.
- **🎵 Плейлист дня** — ссылка на Яндекс.Музыку (меняется в `.env`).
- **🎲 Рандомный факт** — факты без повторов для каждого пользователя.
- **🧠 Тесты:**
  - *Музыкант по знаку зодиака* — 12 знаков → музыкант с описанием.
  - *Какой стиль тебе подходит* — тест с подсчётом очков (на FSM).
  - *Квест «Спаси концерт»* — ветвящийся сюжет (универсальный движок графа).
- **📅 Мероприятия** — список из `content/events.json`.
- **💬 Общение** — ссылка на чат сообщества.
- **/stats** — статистика для админов (всего/новых сегодня/подписаны).

## Технологии

- Python 3.11+
- [aiogram 3.x](https://docs.aiogram.dev/) (async, роутеры, встроенный FSM)
- aiosqlite + SQLite (локальная база, без внешних сервисов)
- python-dotenv (секреты в `.env`)
- Режим: **long polling** (см. раздел про webhook ниже)

## Структура

```
main.py                  # запуск: логирование, бот, диспетчер, polling
config.py                # чтение .env + загрузка контента из JSON
requirements.txt
.env.example
handlers/
  start.py               # /start + гейт подписки
  menu.py                # главное меню, плейлист, чат, UI-помощники
  facts.py               # рандомные факты без повторов
  tests.py               # выбор теста + движки зодиака, стиля и квеста
  events.py              # мероприятия
  admin.py               # /stats
keyboards/kb.py          # все inline-клавиатуры
middlewares/subscription.py  # проверка подписки на каждое действие
database/db.py           # инициализация и запросы к SQLite
states/states.py         # FSM-состояния для теста на стиль и квеста
content/                 # весь контент в JSON (правится без программиста)
  facts.json
  quiz_zodiac.json
  quiz_style.json
  quest_concert.json     # нужно добавить (см. ниже)
  events.json            # нужно добавить (см. ниже)
```

**Принцип:** вся логика — в коде, весь контент — в `content/*.json` и `.env`.
Чтобы добавить факты, поменять плейлист или отредактировать тест, Python трогать не нужно.

## Подготовка перед запуском

1. **Создай бота** у [@BotFather](https://t.me/BotFather) → получи токен.
2. **Сделай бота администратором канала** @blackmagicwoman.
   Это обязательно: без прав админа метод `get_chat_member` не сможет проверять
   подписку, и гейт будет закрыт для всех. Достаточно минимальных прав
   (главное — чтобы бот видел участников).
3. **Узнай свой Telegram id** у [@userinfobot](https://t.me/userinfobot) — для `/stats`.
4. Приготовь ссылки: на плейлист в Яндекс.Музыке и на чат сообщества.

## Установка и запуск

```bash
# 1. Виртуальное окружение (если ещё нет)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Зависимости
pip install -r requirements.txt

# 3. Настройки
cp .env.example .env
# открой .env и заполни BOT_TOKEN, CHANNEL_ID, ссылки, ADMIN_IDS

# 4. Запуск
python main.py
```

Бот запускается командой `python main.py` без правок кода — нужно только заполнить `.env`.
База `bot.db` и лог `bot.log` создаются автоматически рядом с `main.py`.

## Переменные `.env`

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен от @BotFather (обязательно) |
| `CHANNEL_ID` | `@username` или числовой `-100...` (обязательно) |
| `CHANNEL_URL` | ссылка на канал (кнопка «Подписаться») |
| `COMMUNITY_CHAT_URL` | ссылка на чат сообщества |
| `YANDEX_PLAYLIST_URL` | ссылка на плейлист дня |
| `ADMIN_IDS` | id админов через запятую |
| `RESET_FACTS_WHEN_DONE` | `true` — крутить факты по кругу; `false` — показать «закончились» |

## Редактирование контента

Все файлы в `content/` — обычный JSON в UTF-8. После правки перезапуск не нужен:
бот читает файлы заново при каждом запросе.

- **Картинка меню** — положи файл `content/menu.jpg` (или `.png`), и он покажется баннером
  над главным меню. Можно задать другой путь или URL через `MENU_IMAGE` в `.env`.
  Нет файла — меню просто текстовое.
- **`facts.json`** — список фактов: `[{"id": 1, "text": "..."}, ...]`. У каждого факта
  уникальный `id` (по нему бот помнит, что пользователь уже видел).
- **`quiz_zodiac.json`** — `{"Овен": {"name": "...", "desc": "..."}, ...}` (все 12 знаков).
- **`quiz_style.json`** — вопросы с очками по стилям и блок `results`. Формат — внутри файла.
- **`quest_concert.json`** — граф квеста. Узлы (`nodes`) бывают трёх видов:
  ```json
  {
    "start": "S0",
    "nodes": {
      "S0": {
        "text": "Текст ситуации.\n\nЧто делаешь?",
        "choices": [
          {"text": "Вариант А", "next": "S_NEXT"},
          {"text": "Вариант Б", "next": "ENDING_BAD"}
        ]
      },
      "S_NEXT": {
        "text": "Промежуточная сцена без выбора.",
        "next": "ENDING_GOOD"
      },
      "ENDING_GOOD": {
        "ending": true,
        "title": "Название концовки",
        "text": "Финальный текст.",
        "verdict": "Что в итоге.",
        "rank": "Звание игрока",
        "rarity": "Очень редкая",
        "score": "10/10"
      },
      "ENDING_BAD": { "ending": true, "title": "Провал", "text": "...", "rarity": "Обычная", "score": "3/10" }
    }
  }
  ```
  - **Сюжетный узел** — `text` + `choices` (кнопки ведут на `next`).
  - **Нода-проходка** — `text` + одно поле `next` (без `choices`): бот покажет одну кнопку «Дальше →».
  - **Концовка** — `"ending": true`. Поля `title`, `verdict`, `rank`, `rarity`, `score` необязательны
    (чего нет — просто не покажется). Бот считает открытые концовки и пишет «Открыто: N/всего».
  - Текст пишется **обычным текстом, без HTML** (бот сам экранирует и оформляет). Id узлов держи
    короткими — они уходят в `callback_data`, лимит Telegram 64 байта.
- **`events.json`** — список мероприятий (нужно создать):
  ```json
  [
    {"title": "Квартирник", "date": "Пятница, 20:00", "place": "Бар «Чёрная кошка»", "url": ""}
  ]
  ```

> Пока `events.json` нет — раздел «Мероприятия» показывает аккуратную заглушку и бот не падает.
> Как добавишь файл — заработает само.

## Переключение на webhook

По умолчанию бот работает на long polling — это проще: процесс сам опрашивает Telegram,
внешний адрес не нужен. Для продакшена иногда удобнее webhook (Telegram сам шлёт апдейты
на твой HTTPS-адрес). Нужен публичный домен с валидным SSL.

Замени в `main.py` запуск polling на webhook-сервер (aiohttp идёт в комплекте с aiogram):

```python
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = "https://ТВОЙ_ДОМЕН" + WEBHOOK_PATH

async def main() -> None:
    setup_logging()
    config.validate()
    await db.init_db()
    bot = create_bot()
    dp = create_dispatcher()
    await bot.set_webhook(WEBHOOK_URL)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=8080)
```

При long polling, наоборот, убедись, что webhook снят: `await bot.delete_webhook()`.

## Хостинг

Для long polling подойдёт любой дешёвый VPS или платформа вроде Railway/Render.
Главное — процесс `python main.py` должен работать постоянно (используй systemd,
`screen`/`tmux` или менеджер процессов платформы).

## Модель данных (SQLite)

- `users(user_id, username, full_name, first_seen, last_active, is_subscribed)`
- `seen_facts(user_id, fact_id, seen_at)` — какие факты кто видел
- `quiz_results(user_id, quiz_name, result, completed_at)` — результаты тестов/квеста
