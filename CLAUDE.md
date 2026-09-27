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
pip install -r requirements.txt  # aiogram, aiosqlite, python-dotenv, aiohttp
cp .env.example .env             # then fill BOT_TOKEN, CHANNEL_ID, ADMIN_IDS, links
python main.py                   # runs the bot (long polling) + the Mini App web server
```

Run `python main.py` from this directory — `config.py` resolves `content/`, `.env`,
`bot.db`, and `bot.log` relative to `main.py`'s location (`BASE_DIR`), so paths only
line up when run as `main.py`. `main.py` also starts an aiohttp server (`server.py`,
port `config.PORT`, default 8080) for both Mini Apps and the dashboard — same process, see
"Mini App server" below. Without `WEBAPP_URL` (or `RENDER_EXTERNAL_URL`) set, the server
still runs but the Mini App buttons just don't appear in the menus.

There is **no test suite and no configured linter** in this repo — don't assume `pytest`
or `ruff` commands exist. Verification is manual (run the bot, drive it in Telegram). One
cheap check needs no token or network: it imports every module and builds both the
aiohttp app and the dispatcher, catching syntax errors, circular imports (see "Band
tournament") and route/router wiring mistakes:

```bash
python -c "import main, server; server.create_app(); main.create_dispatcher()"
```

**Single-instance rule:** Telegram allows only one `getUpdates` consumer per token.
Never run two `python main.py` against the same `BOT_TOKEN` (e.g. a local instance plus
a deployed one) — the second gets `409 Conflict`. `start_polling(drop_pending_updates=True)`
makes restarts safe.

With the bot running, the web pages also open in a plain browser:
`http://localhost:8080/rockle/`, `/tournament/`, `/dashboard/` (log in with
`DASHBOARD_TOKEN`). The games load and play (`/api/*/today` works anonymously), but
`POST .../complete` returns 401 without Telegram's signed `initData` — recording a result
needs the real Telegram client (HTTPS via `WEBAPP_URL`, e.g. an ngrok tunnel).

## Deployment

Production is a Render Blueprint, `render.yaml`: a single `web` service running
`python main.py` (health check `GET /healthz`) with a persistent disk at `/data` —
`DB_PATH=/data/bot.db`, `PLAYLISTS_PATH=/data/playlists.json`. Secrets are declared there
with `sync: false` (Render asks for the values once), so a new env var production needs
belongs in `render.yaml` too, not just `.env.example`. `WEBAPP_URL` is deliberately absent —
`config.py` falls back to Render's own `RENDER_EXTERNAL_URL`. `DEPLOY_PLAN.md` (Docker/VPS)
is an unimplemented plan that predates the aiohttp server; its "no inbound port needed"
premise no longer holds.

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
   everyone out. The gate only sees aiogram updates: the aiohttp routes (Mini Apps,
   public leaderboard, dashboard) are outside it. The Mini Apps are "gated" only because
   their `web_app` buttons live behind the gate, so anything that must be subscriber-only
   server-side has to check `users.is_subscribed` itself.

### Wiring (main.py)

`create_dispatcher()` registers routers in a deliberate order:
`start → menu → facts → tests → events → admin → **fallback**`. Routers are checked in
include order, so `handlers/fallback.py` (a catch-all `@router.callback_query()`) must
stay **last** — it answers stale buttons (e.g. a tap on an old message after the FSM
state was cleared) so Telegram doesn't spin. Putting it earlier would swallow real
callbacks.

### Mini App server (server.py)

The "Найди группу" (word search) game is a Telegram Mini App, not an aiogram handler —
it's a self-contained static page (`webapp/rockle/index.html`) served by an aiohttp app
(`server.py`) that `main.py` starts **in the same process and event loop** as the bot's
long polling, not as a separate service. This is deliberate: Render's persistent disk
(`/data`, holding `bot.db` and `playlists.json`) can only be mounted by one service, so a
second process couldn't reach the same DB/files anyway. If you ever add webhook mode
(see README "Переключение на webhook"), register it on the *same* `web.Application` that
`server.py:create_app()` builds — don't spin up a second aiohttp app.

- **Daily puzzle, shared across users.** `GET /api/rockle/today` deterministically picks
  `WORDS_PER_DAY` (10) bands from `content/rockle_words.json` via
  `random.Random(today_iso).sample(...)` — the same date seeding the playlist queue uses
  once it runs out. The letter grid itself (12×12, horizontal/vertical words only) is built
  **client-side**, seeded from that same date string (FNV-1a hash → mulberry32 PRNG in
  JS), so every player gets a pixel-identical grid that day without the server doing any
  layout work. A word the grid can't fit is silently dropped from that day's puzzle.
  Editing `rockle_words.json` mid-day changes the puzzle for later players (nothing
  fails — `/complete` doesn't check words).
- **Result integrity.** The client posts `initData` (Telegram's signed payload) along
  with the elapsed time to `POST /api/rockle/complete`. `webapp_auth.py:validate_init_data()`
  verifies the HMAC-SHA256 signature (secret = `HMAC_SHA256("WebAppData", BOT_TOKEN)`,
  per Telegram's documented algorithm) and rejects stale `auth_date` (>24h) before trusting
  `user_id` — without this check anyone could POST results under someone else's id. It
  authenticates *who*, not *what*: `seconds` is client-reported and only range-checked
  (1–3600), so best-time rankings trust the client. First completion per
  `(user_id, play_date)` wins (`rockle_results` PK); replays don't overwrite it.
  This check lives in its own module (not in `server.py`) because the tournament Mini App
  needs it too — see "Band tournament" below for why that isn't just an import from `server.py`.
- The Mini App buttons (Rockle in `keyboards/kb.py:tests_menu_kb()`, the tournament in the
  **main** menu via `main_menu_kb()`) only appear when `config.WEBAPP_URL` resolves to
  something — Mini Apps require HTTPS, so there's nothing useful to link to without it.

### Band tournament (tournament.py)

Third self-contained Mini App module (`webapp/tournament/index.html`), alongside
Rockle and the dashboard — `tournament.register_routes()` adds routes onto the same
`web.Application`, the same way `dashboard.register_routes()` does. All 15 comparisons
of the bracket (16 bands → 1/8 → 1/4 → 1/2 → финал) run **client-side**; the server only
hands out today's 16-band draw and accepts the final result.

- **`initData` validation lives in `webapp_auth.py`, not imported from `server.py`.**
  `server.py` does `import dashboard` at module top level; if `tournament.py` imported
  `validate_init_data` from `server.py`, and `server.py` in turn imported `tournament`
  (for `tournament.register_routes(app)` inside `create_app()`) — that's a circular
  import, failing at bot startup. The check itself has no dependency on anything in
  `server.py` (just `hashlib`/`hmac`/`json`/`datetime`/`urllib.parse`), so the module
  split costs nothing; `server.py` and `tournament.py` both import it the same way.
- **Bracket integrity is checked for real, not just "these keys were in yesterday's
  pool."** The server knows today's 16-band draw *and* pairing order (the stored draw is
  in bracket order — pairs are `(draw[0],draw[1]), (draw[2],draw[3])...`), so
  `POST /api/tournament/complete` cheaply re-derives whether the claimed winners are
  actually valid pairwise winners at every round. One helper (`tournament.py:_round_winners_valid()`) is reused 4 times: today's
  16 → round-of-16 winners → quarterfinal winners → semifinal winners → champion.
  Without this, a forged request could crown a band that lost in round one, or one that
  wasn't even in today's draw — not just a band absent from the pool entirely.
- **Points are awarded by furthest stage reached in that run, not cumulatively.**
  Reaching 1/4 финала = 1 point, 1/2 = 2, финал = 3, champion = 5; knocked out in 1/8 = 0,
  never logged at all. This reads the owner's original spec ("за 1/4 1 балл, за 1/2 2
  балла...") the natural way — the standard payout table for bracket tournaments (a
  finalist who loses gets exactly 3 points, not 1+2+3=6). `db.get_band_points_totals()`
  never stores zero rows, so the public leaderboard is zero-filled against the full
  `content/tournament_bands.json` list in `tournament.py`, not in the DB layer — otherwise
  bands that never won a match would be missing from the table instead of sitting at the
  bottom with 0.
- **`add_static()` on a missing directory crashes the whole bot at startup, not just
  the tournament.** `content/tournament_covers/` (band photos) is served via
  `aiohttp.web.Application.router.add_static()`; aiohttp resolves that path with
  `strict=True` synchronously inside `create_app()` — i.e. during `main.py` startup,
  before `dp.start_polling()`. If the owner hasn't created the photo folder yet (likely —
  it's the slowest content to prepare), the bot won't start at all. So
  `tournament.register_routes()` does
  `config.TOURNAMENT_COVERS_DIR.mkdir(parents=True, exist_ok=True)` before `add_static()`
  — the same trick `config.seed_playlists()` already uses for the playlist queue.
- **The leaderboard route is genuinely public.** `GET /api/tournament/leaderboard` checks
  neither `initData` nor `DASHBOARD_TOKEN` — this is content for every user, not an
  owner-only surface like `/api/dashboard/*`.
- **One completed run per day counts for points, but doesn't hard-block replay.** If
  `db.get_quiz_result_today(user_id, "tournament")` already returns a value, `POST
  /complete` answers `409 already_completed` with the recorded champion and awards
  nothing again. Unlike Rockle (where a repeat attempt has a meaningful "better/worse" by
  time, and the first result quietly wins) different tournament runs simply aren't
  comparable — refusing outright is more honest than picking one arbitrarily. The client
  doesn't block replay, it just doesn't count a second time (an "already played today"
  banner, no hard stop). The check-then-write runs under a module-level `asyncio.Lock`
  (`_complete_lock`) — otherwise a burst of parallel requests passes the check together and
  awards points several times. The lock is only enough because of the single-process
  rule; multiple workers would need a DB-level uniqueness constraint instead.
- **The daily draw is a rotation, computed once and stored.** The first request of the
  day runs `_make_draw()` and saves the result in `tournament_draws` (PK `play_date`;
  `INSERT OR IGNORE` + re-read, so concurrent first requests agree); every later request,
  including `/complete`'s validation, reads the stored draw (`_today_draw()`). Selection
  priority: longest since last played (absent from the window = first) → fewest plays in
  the last `HISTORY_DAYS` (14) → random; then the 16 are shuffled into pairs. With ≤32
  bands that guarantees "absent yesterday → in today" and evens out appearances — the old
  pure-random draw gave 2–7 appearances per band in the first 9 days, which is what
  skewed the points. Because the draw is frozen, editing `tournament_bands.json` takes
  effect the next day without voiding in-flight runs; only a run straddling UTC midnight
  still gets `400 invalid_bracket` — silently, since the client ignores `/complete`'s
  response.
- **The rotation's first start backfilled history** (`_backfill_legacy_draws()`, runs
  only while `tournament_draws` is empty): pre-rotation days are re-created with the old
  formula `random.Random(date).sample(pool, 16)` so the balancing knows who was
  under-drawn. Today gets the old formula too only if the tournament was already opened
  today (those players hold the old bracket); otherwise the rotation applies at once.
- **The champion-screen party promo is hardcoded** in `webapp/tournament/index.html`
  (ticket link + date), an exception to the content-in-JSON rule: it duplicates the
  `events.json` entry without reading it, so update or remove it by hand when the event
  changes or passes. Clicks hit `POST /api/tournament/promo`, logged as feature
  `party_promo` — counted in `/stats`/`/month` and queryable by the dashboard API as
  `feature:party_promo` (the dashboard's "Турнир групп" panel shows its window total).
- Band pool: `content/tournament_bands.json` (flat array of `{"key", "display", "photo"}`;
  `key` is a lowercase slug, closer in role to `quiz_musician.json`'s result keys than to
  `rockle_words.json`'s uppercase letters). Needs **at least 16** bands — fewer and
  `/today`/`/complete` return an empty result / a clear error instead of crashing.

### Analytics dashboard (dashboard.py)

`/dashboard` is a private, owner-only analytics website — plain browser, not a Telegram
surface. Same deal as the Mini App: its routes (`dashboard.py:register_routes()`) are
registered onto the *same* `web.Application` inside `server.py:create_app()`, not a
second app/port. `webapp/dashboard/index.html` follows the exact same convention as
`webapp/rockle/index.html` — one self-contained file, inline CSS/JS, no build step, one
pinned CDN script (Chart.js) for the one full-size chart, plus a Google Fonts stylesheet
(Doto with `ROND=100` for the dot-matrix numbers, Manrope for UI — Manrope because it has
Cyrillic). Everything else (KPI ring tiles, metric panels, sparklines) is hand-rolled inline
SVG to avoid instantiating many Chart.js instances. The look is a single "frosted glass
over a blurred photo" theme — no light/dark variants; the background is CSS blobs, not an
image file.

- **Every number respects the selected window (7/30/90 days).** `db.get_kpi_summary(days)`
  computes average DAU, unique actives and new users over the window (plus delta vs. the
  previous window of equal length) — don't reintroduce "today"-only or fixed-30-day values
  next to the range switcher; that mismatch with `/month` was a reported bug. The only
  non-windowed numbers are live snapshots (total users, currently subscribed). For the
  same reason the tournament panel uses `/api/dashboard/tournament-bands`
  (`db.get_band_points_between()`), not the public all-time `/api/tournament/leaderboard`.
- **Release markers are drawn on the main chart** by an inline Chart.js plugin
  (`releaseMarkers` in the page) — it matches `releases.released_at` days against
  `chart.$isoDays`, because the axis labels are `дд.мм` strings.

- **Auth is a shared-secret token, not Telegram identity.** `config.DASHBOARD_TOKEN`
  compared via `hmac.compare_digest` against an `Authorization: Bearer` header
  (`dashboard.py:check_dashboard_auth()`). Empty token → every `/api/dashboard/*` route
  returns 401 and the page just shows an unusable login form — same "degrade quietly,
  don't crash the bot" pattern as `WEBAPP_URL`/`MENU_IMAGE`. This is a deliberate
  departure from the `ADMIN_IDS` pattern used everywhere else: the dashboard is meant to
  open in a plain desktop browser, not launched from inside Telegram.
- **Subscriber growth needed a new log, because `users.is_subscribed` has no history.**
  It's a live flag, overwritten in place — there was never a way to know *when* someone
  (un)subscribed, only their current state. `set_subscribed()` now reads the current value
  first and only appends to `subscription_events(user_id, is_subscribed, changed_at)` when
  it actually changes (not on every gate re-check, which is most bot interactions — that
  would make the table huge for no signal). `init_db()` runs a one-time
  `_backfill_subscription_events()` that seeds one synthetic "subscribed" event per
  already-subscribed user so the running-total chart (`db.get_subscriber_growth()`) has a
  sane starting point instead of jumping from zero. The unavoidable side effect: the chart
  always shows one big step on day one (everything accumulated before tracking started),
  then true day-by-day history after that — this is stated in the dashboard UI, not hidden.
- **Releases are marked by hand — there's no way to infer them.** Content and code ship in
  the same git commits with no versioning convention (see "Content files" below), so
  `releases(id, label, kind, released_at, note, created_at)` exists purely because the
  owner types a label into a dashboard form when they ship something. The before/after
  traffic delta (`dashboard.py:_compute_impact()`) compares a metric's average (the page
  asks for DAU over 7-day windows) in the N days before vs. after `released_at` — one
  query per side via `db.get_daily_metric()`, which is also what powers every
  chart/sparkline (dispatches on a `metric` string: `"dau"`, `"new_users"`,
  `"feature:<name>"`, `"quiz:<name>"`, `"quiz_total"`, `"rockle_completed"`).
- **No giveaway-entry mechanism exists on purpose.** An earlier design had users tap
  "участвовать" in the bot and an automated winner draw; that was cut as overbuilt. What
  shipped instead is `db.get_leaderboard()` — a read-only ranking (`metric="rockle"`: most
  `rockle_results` plays in a date range, tie-broken by best time; `metric="active"`: most
  `daily_active` days) that the owner reads manually to pick a winner. A giveaway is just a
  `releases` row with `kind="giveaway"`; announcing it is a normal `/broadcast`, not
  anything dashboard-triggered — there's no code path where the dashboard sends Telegram
  messages.

### Handlers and their callback_data

`keyboards/kb.py` is the single source of truth for every inline keyboard and its
`callback_data` string (the header comment lists them all). The dispatch contract is
`F.data == "..."` / `F.data.startswith("...")` in handlers matched against these strings.
Note Telegram's **64-byte `callback_data` limit** — quest node ids and zodiac indices ride
inside `callback_data`, so keep them short.

- `handlers/menu.py` — main menu + simple link screens (playlist, chat). Owns the shared
  UI helpers `safe_edit()` and `show_main_menu()` imported across other handlers.
- `handlers/facts.py` — random fact, no repeats per user (`seen_facts` table).
- `handlers/tests.py` — four quizzes: zodiac (stateless lookup), "guess the band by
  album cover" (FSM, cover tests `quiz_covers_1/2/3.json` — parts 1–2 have 15 questions,
  part 3 has 16; each question is a photo of an album cover with band-name options, scored),
  "which rock/metal musician are you" (FSM, `quiz_musician.json` — 10 text questions, each
  option casts a point for a musician key; highest score wins, ties broken by
  `random.choice`), and the "Save the concert" quest (FSM, generic graph engine driving
  `quest_concert.json`). The cover quiz reads its data by the `key` in `callback_data`, so a
  **new part is a `quiz_covers_N.json` file plus a button in `tests_menu_kb()` — no handler
  change** (add `covers_N` to `admin.py:TEST_LABELS` for a readable `/stats` label); the
  musician quiz's questions/options need no code at all. Cover questions are sent as a
  **new photo message** each time (delete-and-resend, since a photo message can't be
  edited into text), with the answer shown via `edit_caption` and each cover's `file_id`
  cached after first upload (`_cover_file_id_cache`); the musician quiz is plain text, so
  it edits screens in place via `safe_edit()` instead.
- `handlers/admin.py` — `ADMIN_IDS`-only: `/stats`, `/month`, `/playlists`, `/backup`,
  `/broadcast` (+ `/cancel`), and **playlist upload** (admin sends a `.json` document; it's
  appended to the queue, deduped by url, written atomically via `tmp.replace`). There's no
  router-level filter: every handler checks `config.ADMIN_IDS` itself and silently returns
  for everyone else — a new admin command must do the same. `/stats` (today, UTC) and
  `/month` (last 30 days, plus MAU/average DAU) print counters from `db.get_stats()` /
  `db.get_month_stats()`; completed runs are grouped by `quiz_name`, with `TEST_LABELS`
  mapping known names to Russian labels (unknown names print raw). `/broadcast` is a small
  FSM (`states.Broadcast`: `awaiting_content` → `confirming`) — admin sends any message
  (text/photo/video/whatever), bot shows a recipient count + confirm/cancel buttons, then
  fans it out to every `users.user_id` via `bot.copy_message()` (so it doesn't need to
  parse content types itself) with a small per-message delay and `TelegramRetryAfter`
  handling to stay under Telegram's flood limits; `TelegramForbiddenError` (user blocked
  the bot) is counted separately, not treated as a failure. The draft-capture handler is
  registered **before** the bare `F.document` playlist-upload handler and filtered to
  ignore anything starting with `/` — otherwise it would swallow either a broadcasted
  document or an unrelated admin command typed mid-flow (handlers in a router match in
  registration order, first filter match wins).

### The photo-banner editing gotcha (menu.py)

The main menu can show an image banner (`content/menu.*` or `MENU_IMAGE`). A Telegram
photo message **cannot be edited into a text message** and vice versa. So `safe_edit()`
and `show_main_menu()` delete-and-resend when crossing the photo↔text boundary, and only
`edit_text`/`edit_caption` in place otherwise (swallowing "message is not modified").
Any new screen transition must go through these helpers, not raw `edit_text`. The banner
`file_id` is cached in a module global after first upload to avoid re-uploading the file.
Consequence of both `file_id` caches (`_banner_file_id`, `tests.py:_cover_file_id_cache`):
JSON edits are live, but **an image replaced under the same filename isn't picked up until
a restart** — the old cached `file_id` keeps being sent. For covers, use a new filename and
update the quiz JSON instead.

### State and data

- **FSM:** `states/states.py` defines `CoverQuiz.answering`, `MusicianQuiz.answering`,
  `Quest.playing` and the admin `Broadcast` flow; storage is in-memory (`MemoryStorage`), so
  restarting the bot drops in-progress tests/quests (and broadcast drafts).
  Handlers `state.clear()` on returning to menu and on finishing.
- **DB:** `database/db.py` holds a *single* shared `aiosqlite` connection (`_db` global)
  for the whole process, opened in `init_db()` and closed in `close_db()`. All access goes
  through its async functions — don't open new connections. The whole schema is in
  `init_db()` (README «Модель данных» describes each table). **There are no migrations:**
  `init_db()` runs `CREATE TABLE/INDEX IF NOT EXISTS` on every start, so a new table or
  index just appears, but a new *column* on an existing table never reaches the production
  DB (`/data/bot.db`) without an explicit, idempotent `ALTER TABLE` (guard it with
  `PRAGMA table_info`). One-time data fixes follow `_backfill_subscription_events()`:
  called from `init_db()`, a no-op once done.
- **`daily_active` (the DAU/MAU source) is written only as a side effect of
  `upsert_user()` / `set_subscribed()`** — i.e. by `/start` and the subscription gate.
  Mini App requests and admins (who bypass the gate) don't count toward DAU.
- **Analytics keys are stored strings — don't rename them.** `feature_usage.feature`
  (`playlist`, `rockle_open`, `tournament_open`, `party_promo`) and `quiz_results.quiz_name`
  (`zodiac`, `covers_<N>`, `musician`, `quest_concert`, `tournament`) are what `/stats`,
  `TEST_LABELS` and the dashboard's `feature:<name>` / `quiz:<name>` metrics key off;
  renaming one splits its history in two.
- **All timestamps are UTC** (`_now()`, SQLite `DATE('now')`, `webapp_auth.today_iso()`);
  keep new date logic UTC. Don't copy the exceptions: `dashboard.py` and
  `db.get_subscription_flow()` use `date.today()` (host-local time) —
  harmless on a UTC host, but off by the local offset around midnight elsewhere (e.g. on a
  dev machine).
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
`quiz_zodiac.json` (12 signs), `quiz_covers_1.json`/`quiz_covers_2.json`/`quiz_covers_3.json`
(`{title, questions:[{photo, group, album, options, correct}]}`; `photo` names a file in
`content/covers/`, `correct` indexes `options`),
`quiz_musician.json` (`{title, questions:[{text, options:[{text, result}]}], results:{key:{emoji,
name, desc}}}`; each option's `result` casts a point for that key in `results`),
`quest_concert.json` (branching graph: story node = `text`+`choices`, pass-through =
`text`+`next`, ending = `"ending": true` + optional `title`/`verdict`/`rank`/`rarity`/`score`),
`rockle_words.json` (flat array of `{display, key}` for the "Найди группу" Mini App; `key`
is uppercase letters only, no spaces/punctuation — that's what gets placed in the grid —
and **at most 12 letters**, the grid `SIZE` in `webapp/rockle/index.html`: a longer key can
never be placed and silently drops out of the puzzle), `tournament_bands.json` (see "Band
tournament"), and `events.json` (optional; absent → placeholder). The README documents each
format in detail.

Not source of truth: `lectures/` is a Russian-language aiogram course built around this bot
at its initial commit — it predates the Mini Apps, dashboard, broadcast and musician quiz,
so treat it as teaching material, not a spec. Root-level `upload_*.json` (sample payloads
for the admin playlist upload), `JIm.jpeg` and the `.docx` are owner material that no code
reads.
