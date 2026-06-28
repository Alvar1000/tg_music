# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Scope constraint (important)

The git project is `tg_music/`, and it lives *inside* a Python virtualenv at
`/Users/alfa/envs/tg_env` — the venv's own `bin/`, `lib/`, `include/`, and `pyvenv.cfg`
are siblings of `tg_music/`. **Work only inside `tg_music/`.** Do not modify the
surrounding virtualenv, do not `pip install` into the global/system Python, and do not
read or edit files outside `tg_music/`.

User-facing strings and code comments are in Russian by design (the bot serves a
Russian-speaking rock community). Keep new UI text and comments in Russian to match.

## Commands

```bash
# from /Users/alfa/envs/tg_env/tg_music
source ../bin/activate           # activate the surrounding venv (Python 3.13)
pip install -r requirements.txt  # aiogram, aiosqlite, python-dotenv only
cp .env.example .env             # then fill BOT_TOKEN, CHANNEL_ID, ADMIN_IDS, links
python main.py                   # runs the bot (long polling)
```

Run `python main.py` from this directory — `config.py` resolves `content/`, `.env`,
`bot.db`, and `bot.log` relative to `main.py`'s location (`BASE_DIR`), so paths only
line up when run as `main.py`.

There is **no test suite and no configured linter** in this repo — don't assume `pytest`
or `ruff` commands exist. Verification is manual (run the bot, drive it in Telegram).

**Single-instance rule:** Telegram allows only one `getUpdates` consumer per token.
Never run two `python main.py` against the same `BOT_TOKEN` (e.g. a local instance plus
a deployed one) — the second gets `409 Conflict`. `start_polling(drop_pending_updates=True)`
makes restarts safe.

## Architecture

aiogram 3.x Telegram bot. Two hard rules shape everything:

1. **Logic lives in code; content lives in `content/*.json` and `.env`.** The channel
   owner edits JSON/`.env` without touching Python. `config.load_content()` re-reads the
   file on *every* request, so content edits apply with no restart, and a missing/broken
   file degrades to a placeholder (returns `default`) instead of crashing.
2. **A subscription gate fronts all functionality.** `middlewares/subscription.py`
   (`SubscriptionMiddleware`) is an *outer* middleware on both `dp.message` and
   `dp.callback_query`. Before any handler runs it calls `get_chat_member` on the channel,
   writes the result to `users.is_subscribed`, and blocks non-subscribers with the gate
   screen. Only `/start`, the `check_sub` callback, and `ADMIN_IDS` bypass it. The bot
   **must be an admin of the channel** or `get_chat_member` fails and the gate locks
   everyone out.

### Wiring (main.py)

`create_dispatcher()` registers routers in a deliberate order:
`start → menu → facts → tests → events → admin → **fallback**`. Routers are checked in
include order, so `handlers/fallback.py` (a catch-all `@router.callback_query()`) must
stay **last** — it answers stale buttons (e.g. a tap on an old message after the FSM
state was cleared) so Telegram doesn't spin. Putting it earlier would swallow real
callbacks.

### Handlers and their callback_data

`keyboards/kb.py` is the single source of truth for every inline keyboard and its
`callback_data` string (the header comment lists them all). The dispatch contract is
`F.data == "..."` / `F.data.startswith("...")` in handlers matched against these strings.
Note Telegram's **64-byte `callback_data` limit** — quest node ids and zodiac indices ride
inside `callback_data`, so keep them short.

- `handlers/menu.py` — main menu + simple link screens (playlist, chat). Owns the shared
  UI helpers `safe_edit()` and `show_main_menu()` imported across other handlers.
- `handlers/facts.py` — random fact, no repeats per user (`seen_facts` table).
- `handlers/tests.py` — three quizzes: zodiac (stateless lookup), style test (FSM,
  accumulates per-style scores), and the "Save the concert" quest (FSM, generic graph
  engine driving `quest_concert.json`).
- `handlers/admin.py` — `ADMIN_IDS`-only: `/stats`, `/playlists`, and **playlist upload**
  (admin sends a `.json` document; it's appended to the queue, deduped by url, written
  atomically via `tmp.replace`).

### The photo-banner editing gotcha (menu.py)

The main menu can show an image banner (`content/menu.*` or `MENU_IMAGE`). A Telegram
photo message **cannot be edited into a text message** and vice versa. So `safe_edit()`
and `show_main_menu()` delete-and-resend when crossing the photo↔text boundary, and only
`edit_text`/`edit_caption` in place otherwise (swallowing "message is not modified").
Any new screen transition must go through these helpers, not raw `edit_text`. The banner
`file_id` is cached in a module global after first upload to avoid re-uploading the file.

### State and data

- **FSM:** `states/states.py` defines `StyleTest.answering` and `Quest.playing`; storage is
  in-memory (`MemoryStorage`), so restarting the bot drops in-progress tests/quests.
  Handlers `state.clear()` on returning to menu and on finishing.
- **DB:** `database/db.py` holds a *single* shared `aiosqlite` connection (`_db` global)
  for the whole process, opened in `init_db()` and closed in `close_db()`. All access goes
  through its async functions — don't open new connections. Tables: `users`, `seen_facts`,
  `quiz_results`, `seen_endings`, `playlist_state`. **All timestamps are UTC** (`_now()`,
  and SQLite `DATE('now')`); keep new date logic UTC to stay consistent.
- **Playlist-of-the-day** is a shared rotating queue. `playlist_state` (single row, id=1)
  holds a pointer that advances by `+1` per elapsed calendar day (UTC) and clamps at the
  end of the queue until an admin uploads more. The queue file is `config.PLAYLISTS_PATH`
  (default `content/playlists.json`; point it at a persistent disk like `/data/playlists.json`
  in production — see README "Плейлисты: очередь и поведение на сервере"). `config.seed_playlists()`
  copies the repo file onto the disk **once**, on first boot when the disk file is absent;
  after that the disk copy wins and repo/git edits no longer change the live queue — update
  it via the admin `.json` upload. Admin `/backup` sends a `VACUUM INTO` copy of `bot.db` to
  the requesting admin.

### Output conventions

`ParseMode.HTML` is the bot default. Every piece of user- or content-derived text is run
through `html.escape()` before interpolation into HTML — follow this when adding screens.
Quest/content JSON is authored as **plain text** (no HTML); the code escapes and formats it.

## Content files (`content/`)

Edited live, UTF-8, re-read per request. `facts.json` (objects with stable `id`),
`quiz_zodiac.json` (12 signs), `quiz_style.json` (scored questions + `results`),
`quest_concert.json` (branching graph: story node = `text`+`choices`, pass-through =
`text`+`next`, ending = `"ending": true` + optional `title`/`verdict`/`rank`/`rarity`/`score`),
and `events.json` (optional; absent → placeholder). The README documents each format in
detail. `DEPLOY_PLAN.md` is a not-yet-implemented Docker/VPS deployment design.
