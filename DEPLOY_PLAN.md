# План: Dockerизация и деплой бота `tg_music`

> Статус: план (файлы инфраструктуры ещё не созданы). Документ проектирует обёртку
> репозитория в Docker для деплоя на сервере.

## 1. Что деплоим и главные ограничения

`tg_music` — Telegram-бот на **aiogram 3.28** (Python 3.11+), работает в режиме
**long polling** ([main.py](main.py)). Зависимостей всего три: `aiogram`,
`aiosqlite`, `python-dotenv` ([requirements.txt](requirements.txt)).

Четыре ограничения, которые определяют всю архитектуру контейнера:

| Ограничение | Следствие для Docker |
|---|---|
| **Long polling, исходящие соединения** | Не нужен ни один проброшенный порт, ни reverse-proxy, ни SSL. Только исходящий `443` к `api.telegram.org`. |
| **Telegram разрешает 1 потребителя `getUpdates` на токен** | Запускаем **строго один экземпляр**. Любой `replicas>1` / `--scale` / перекрытие старого и нового контейнера → `409 Conflict`. |
| **SQLite `bot.db` + `content/playlists.json` пишутся в рантайме** | Состояние нужно вынести на **тома (volumes)**, иначе оно теряется при каждом пересоздании контейнера. |
| **Контент редактируется «вживую» без рестарта** (обещание README, плюс админ-загрузка плейлистов) | `content/` — это **bind-mount с хоста**, а образ лишь несёт «семена» по умолчанию. |

`content/` — не только read-only ресурс: [handlers/admin.py:119-123](handlers/admin.py#L119-L123)
при загрузке плейлиста админом перезаписывает `content/playlists.json` на лету
(атомарно через `tmp.replace`), а [config.py:17](config.py#L17) жёстко задаёт
`CONTENT_DIR = BASE_DIR / "content"`.

## 2. Архитектурные решения (с обоснованием)

- **Базовый образ:** `python:3.12-slim-bookworm`, пин до patch-версии + digest
  (`@sha256:…`) для воспроизводимости. **Не Alpine:** транзитивные зависимости
  (`pydantic-core` — Rust, `aiohttp`, `multidict`, `yarl`) имеют готовые
  manylinux/glibc-колёса; на musl-Alpine pip начнёт собирать их из исходников
  (нужны gcc/cargo). На slim — ноль инструментов сборки.
- **Сборка:** одностадийная. Multi-stage не даёт выигрыша, т.к. компиляторов в
  процессе нет. `pip install --no-cache-dir`, `requirements.txt` копируем и ставим
  **до** копирования кода — слой зависимостей кешируется.
- **Запуск под non-root** с фиксированным `UID:GID` (например `10001:10001`), чтобы
  права совпали с владельцем хостовых каталогов под bind-mount.
- **PID 1 и сигналы:** `init: true` (tini) + **exec-форма** `CMD ["python","main.py"]`.
  Иначе `docker stop` пошлёт `SIGTERM` в `/bin/sh`, тот его не пробросит, и `finally`
  в [main.py:71-74](main.py#L71-L74) (закрытие сессии бота и БД) не выполнится.
  `stop_grace_period: 30s` (> 10s таймаута long-poll), чтобы аккуратно завершить
  текущий `getUpdates` и `db.close_db()`.
- **Логи:** только в stdout (`StreamHandler` уже есть) + ротация драйвером Docker
  `json-file` (`max-size: 10m`, `max-file: 3`). Файловый `FileHandler` в контейнере
  не используем — иначе `bot.log` растёт безгранично на томе. → Потребует мелкой
  правки [main.py:24-26](main.py#L24-L26) или `LOG_FILE`, указывающего в
  `/dev/stdout`/на том (решение — в §8).
- **Без `EXPOSE` и без `TZ`:** портов нет; все даты в коде уже UTC
  ([database/db.py:21](database/db.py#L21), `DATE('now')` в SQLite — UTC), поэтому
  контейнер оставляем на UTC, чтобы логи и БД были согласованы.

## 3. Что персистим (тома)

Два постоянных каталога на хосте, оба `chown 10001:10001`:

| Хост | В контейнере | Что внутри | Зачем том |
|---|---|---|---|
| `/var/lib/tg_music/data` | `/data` | `bot.db` (+ `-wal`/`-shm`) | Все пользователи, подписки, факты, результаты тестов, указатель плейлиста. `DB_PATH=/data/bot.db`. |
| `/var/lib/tg_music/content` | `/app/content` (bind-mount) | `*.json`, `menu.png` | Контент читается каждый запрос **и** перезаписывается при админ-загрузке. Bind-mount позволяет редактировать вживую и переживает пересоздание. |

`DB_PATH` переопределяется через env ([config.py:65](config.py#L65)), а вот
`CONTENT_DIR` **жёстко зашит** ([config.py:17](config.py#L17)) — поэтому монтируем
именно по пути `/app/content`, путь приложения менять не нужно.

**Сидирование контента:** `content/` запекается в образ как значения по умолчанию;
`entrypoint.sh` при первом старте, если хостовый bind-mount пуст, копирует туда
дефолты (`cp -n`). Иначе свежий bind-mount «затенит» запечённый контент пустотой и
бот не найдёт `menu.png`/JSON.

## 4. Артефакты, которые нужно создать

1. **`Dockerfile`** — slim-база (пин + digest), `ENV PYTHONUNBUFFERED=1
   PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1`, non-root `UID 10001`, кеш-слой
   зависимостей, `mkdir/chown /data`, tini + exec-`CMD`.
2. **`.dockerignore`** — критично. Контекст сборки = сам каталог `tg_music` (он лежит
   **внутри** venv `/Users/alfa/envs/tg_env` — родитель в контекст брать нельзя).
   Исключить: `.env`/`.env.*`, `*.db`/`*.db-wal`/`-shm`/`-journal`/`*.sqlite*`,
   `*.log`, `bot.db`, `bot.log`, `**/__pycache__`, `*.py[cod]`, `.git`, `lectures/`
   (учебные доки, 232 КБ, в рантайме не нужны), `upload_playlist.json`
   (неиспользуемый артефакт-однодневка), `.DS_Store`, `.venv/venv`, сам
   `Dockerfile`/`.dockerignore`. **Оставить** `content/` (включая `menu.png` 2.9 МБ —
   это баннер меню) и `.env.example`.
3. **`docker-compose.yml`** — один сервис, без `ports`, `env_file` → серверный `.env`,
   `environment: DB_PATH=/data/bot.db`, два тома (см. §3), `init: true`,
   `stop_grace_period: 30s`, `restart: unless-stopped`, `logging: json-file
   (max-size 10m, max-file 3)`. Комментарий-предупреждение «не масштабировать».
4. **`entrypoint.sh`** — идемпотентное сидирование `content/` при пустом bind-mount,
   проверка доступности `/data`, затем `exec python main.py`.
5. **`.env.production.example`** — серверный шаблон со всеми переменными
   (вкл. `DB_PATH=/data/bot.db`), на реальном файле `chmod 600`.
6. **`deploy/backup.sh` + cron/systemd-timer** — ежедневный онлайн-бэкап
   `sqlite3 .backup` для `bot.db` + копия `content/playlists.json`, ретенция ~14 дней.
7. **`DEPLOY.md`** — операционная инструкция (заменяет раздел про systemd в
   [README.md:188-192](README.md#L188-L192) для Docker-пути).
8. *(опционально)* `.github/workflows/docker-publish.yml` — сборка/пуш в GHCR, если
   уйти от «сборки на сервере» (есть remote `Alvar1000/tg_music`).

## 5. Конфигурация и секреты

- Обязательные переменные: `BOT_TOKEN`, `CHANNEL_ID` — без них `config.validate()`
  ([config.py:69](config.py#L69)) падает с понятной ошибкой и контейнер выходит с
  ненулевым кодом. Остальные (`CHANNEL_URL`, `COMMUNITY_CHAT_URL`,
  `YANDEX_PLAYLIST_URL`, `ADMIN_IDS`, `RESET_FACTS_WHEN_DONE`, `MENU_IMAGE`) —
  опциональные.
- Секреты **только в рантайме** через `env_file: /var/lib/tg_music/.env`
  (`chmod 600`, владелец — деплой-пользователь). Реальный `.env` никогда не попадает
  в образ (исключён `.dockerignore`) и не коммитится.
- `.env` приложения в коде опционален: `config.py` читает `os.getenv` напрямую,
  `load_dotenv` просто не найдёт файл — поэтому можно вообще не класть `.env` в
  контейнер, а передавать `environment`/`env_file`.

## 6. Пошаговый runbook деплоя (с нуля на чистый VPS)

1. **Сервер:** Debian/Ubuntu VPS, установить `docker`, `docker compose plugin`,
   `git`, `sqlite3`; убедиться, что разрешён исходящий `TCP 443`.
2. **Каталоги состояния:** `mkdir -p /var/lib/tg_music/{data,content} && chown -R
   10001:10001 /var/lib/tg_music`.
3. **Код:** `git clone https://github.com/Alvar1000/tg_music.git /opt/tg_music && cd
   /opt/tg_music`, добавить артефакты из §4.
4. **Секреты:** создать `/var/lib/tg_music/.env` (реальные `BOT_TOKEN`, `CHANNEL_ID`,
   ссылки, `ADMIN_IDS`, `DB_PATH=/data/bot.db`), `chmod 600`.
5. **Сид контента (один раз):** `cp -n content/*.json content/menu.png
   /var/lib/tg_music/content/ && chown -R 10001:10001 /var/lib/tg_music/content`
   (либо доверить это `entrypoint.sh`).
6. **Сборка:** `DOCKER_BUILDKIT=1 docker compose build`.
7. **Запуск:** `docker compose up -d`.
8. **Проверка:** `docker compose logs -f` — должны быть «База данных готова» и
   «Бот запускается (long polling)», **без** `RuntimeError` (значит токен/канал
   заданы) и **без** `409 Conflict` (значит второго экземпляра нет). В Telegram:
   `/start` отвечает, `/stats` от админа работает.
9. **Проверка персистентности:** `bot.db` появился в `/var/lib/tg_music/data` с
   владельцем `10001`; загрузка плейлиста админом обновляет
   `/var/lib/tg_music/content/playlists.json` на хосте.

## 7. Эксплуатация: обновление, бэкап, мониторинг

- **Обновление (важно — один потребитель!):** `cd /opt/tg_music && git pull &&
  docker compose up -d --build`. Compose делает *остановить старый → поднять новый*
  (короткий простой), без перекрытия двух `getUpdates`. `drop_pending_updates=True`
  ([main.py:70](main.py#L70)) делает паузу безопасной. **Нельзя** blue-green/rolling
  с одновременной работой двух контейнеров. Откат — `git checkout <prev>` + ребилд.
- **Бэкап:** ежедневный `sqlite3 /var/lib/tg_music/data/bot.db ".backup
  '/var/backups/tg_music/bot-$(date +%F).db'"` (онлайн-`.backup` безопасен на живой
  БД, в отличие от `cp`) + копия `content/playlists.json`; проверить процедуру
  восстановления.
- **Восстановление:** `docker compose down` → положить бэкап в
  `/var/lib/tg_music/data/bot.db` → `docker compose up -d`.
- **Мониторинг:** `restart: unless-stopped` лечит падения; дополнительно — хостовая
  проверка (uptime-kuma / systemd-timer), что контейнер `Up` и логи не «замерли» /
  нет повторяющихся `409`. HTTP-healthcheck невозможен (порта нет); при желании —
  heartbeat-файл (`/tmp/alive`, мелкая правка кода).

## 8. Открытые вопросы (нужно подтвердить перед реализацией)

1. **Логи stdout vs файл.** Рекомендую перейти на stdout-only + ротацию Docker. Это
   требует убрать `FileHandler` в [main.py:24-26](main.py#L24-L26) (или завести
   `LOG_FILE`-условие). Альтернатива без правок кода — направить `LOG_FILE` на том и
   навесить `RotatingFileHandler` (тоже правка). Нужно решение.
2. **Сборка на сервере vs CI/GHCR.** Для одного дешёвого VPS рекомендую «сборку на
   сервере» (проще, без реестра). GHCR — апгрейд на будущее.
3. **`restart` политика.** `unless-stopped` (рекоменд.) против `on-failure:N`: при
   неверном `BOT_TOKEN` `validate()` уводит контейнер в цикл рестартов — это ловится
   проверкой логов на первом деплое, но если хочется «жёсткого стопа при мисконфиге»,
   берём `on-failure:5`.
4. **Судьба `upload_playlist.json` и `lectures/`** — предлагаю исключить из образа
   (первый — неиспользуемый артефакт, второй — учебные доки). Подтвердить, что это ок.
5. **Лёгкая правка кода под контейнер** (пункты 1 и опц. heartbeat) — готовы ли
   вносить, или строго «обернуть как есть» без изменений `main.py`.
