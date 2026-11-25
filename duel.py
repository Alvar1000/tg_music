"""Личные приглашения в рок-дуэль через Telegram Mini App."""
import asyncio
from contextlib import suppress
import logging
import re
import time
from html import escape

from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import config
import webapp_auth
from database import db
from duel_content import cover_catalog, cover_path, intro_catalog, intro_path, load_rounds
from keyboards.kb import duel_invite_kb, duel_play_kb
from middlewares.subscription import member_is_subscribed

logger = logging.getLogger(__name__)
BOT = web.AppKey("duel_bot", Bot)
PAGE = config.BASE_DIR / "webapp" / "duel" / "index.html"


class DuelError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def response(http_status=200, **fields):
    return web.json_response({"server_time": time.time_ns() // 1_000_000, **fields}, status=http_status,
                             headers={"Cache-Control": "no-store"})


async def check_member(bot, user_id):
    try:
        member = await bot.get_chat_member(config.CHANNEL_ID, user_id)
    except (TelegramAPIError, asyncio.TimeoutError) as exc:
        raise DuelError(503, "telegram_unavailable", "Не удалось проверить подписку. Попробуй ещё раз.") from exc
    subscribed = member_is_subscribed(member)
    await db.set_subscribed(user_id, subscribed)
    return member.user, subscribed


@web.middleware
async def authorize(request, handler):
    if not request.path.startswith("/api/duel/"):
        return await handler(request)
    try:
        data = webapp_auth.validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), config.BOT_TOKEN)
        user_id = webapp_auth.extract_user_id(data) if data else None
        if user_id is None:
            raise DuelError(401, "invalid_init_data", "Открой рок-дуэль из меню бота в Telegram.")
        if not await db.duel_user_known(user_id):
            raise DuelError(403, "start_required", "Открой личный чат с ботом и нажми /start.")
        bot = request.app.get(BOT)
        if bot is None:
            raise DuelError(503, "telegram_unavailable", "Бот временно недоступен.")
        user, subscribed = await check_member(bot, user_id)
        if not subscribed:
            return response(403, error="subscription_required", message="Для дуэли нужна подписка на канал.",
                            channel_url=config.CHANNEL_URL)
        if not user.username:
            raise DuelError(403, "username_required", "Для вызова нужен ник Telegram.")
        await db.upsert_user(user.id, user.username, user.full_name)
        request["duel_user"] = user
        return await handler(request)
    except DuelError as exc:
        return response(exc.status, error=exc.code, message=exc.message)


async def page(request):
    return web.FileResponse(PAGE)


async def cover_image(request):
    catalog = cover_catalog()
    index = int(request.match_info["index"])
    path = cover_path(catalog[index]) if index < len(catalog) else None
    if path is None:
        raise web.HTTPNotFound()
    # Порядок в каталоге может измениться при обновлении контента.
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def rating(request):
    return response(**await db.duel_rating(request["duel_user"].id))


async def bootstrap(request):
    uid = request["duel_user"].id
    state = await db.duel_invite_state(uid)
    if state and state["status"] == "active":
        state = await db.duel_game_state(state["id"], uid)
    return response(duel=state)


async def intro_audio(request):
    for track in intro_catalog():
        if track.get("id") == request.match_info["track"]:
            path = intro_path(track)
            if path is not None:
                return web.FileResponse(path, headers={"Content-Type": "audio/mpeg"})
            break
    raise web.HTTPNotFound()


async def invite_response(state, user_id):
    if state["status"] in {"active", "finished"}:
        return response(duel=await db.duel_game_state(state["id"], user_id))
    if state["delivery"] == "failed":
        return response(502, error="delivery_failed", message="Не удалось отправить вызов. Попробуй ещё раз.")
    return response(duel=state)


async def challenge(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        body = None
    if not isinstance(body, dict) or body.get("mode") != "direct" or "rematch_of" in body:
        raise DuelError(400, "invalid_request", "Выбери личный вызов по нику.")
    nickname = body.get("nickname")
    request_id = body.get("request_id")
    if not isinstance(nickname, str) or not re.fullmatch(r"@?[a-zA-Z0-9_]{1,32}", nickname.strip()):
        raise DuelError(400, "invalid_nickname", "Введи ник соперника в Telegram.")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request_id):
        raise DuelError(400, "invalid_request_id", "Открой форму вызова заново.")
    user = request["duel_user"]
    target = nickname.strip().removeprefix("@").lower()
    previous = await db.duel_invite_state(user.id, request_id)
    if previous:
        if previous["target_nickname"] != target:
            raise DuelError(409, "request_conflict", "Этот запрос уже использован для другого вызова.")
        return await invite_response(previous, user.id)
    recipient = await db.duel_recipient_id(target)
    unavailable = DuelError(404, "opponent_unavailable", "Соперник не найден. Попроси его нажать /start в боте.")
    if recipient is None:
        raise unavailable
    if recipient == user.id:
        raise DuelError(400, "self_challenge", "Выбери другого игрока.")
    bot = request.app[BOT]
    peer, subscribed = await check_member(bot, recipient)
    if not peer.username or peer.username.lower() != target:
        raise unavailable
    if not subscribed:
        raise DuelError(403, "opponent_unsubscribed", "Для дуэли сопернику нужна подписка на канал.")
    try:
        duel = await db.create_duel_challenge(user.id, request_id, opponent_nickname=target,
                                              verified_recipient=recipient)
    except ValueError as exc:
        code = str(exc)
        if code == "opponent_unavailable":
            raise unavailable from exc
        errors = {"player_busy": "У одного из игроков уже есть вызов или дуэль.",
                  "request_conflict": "Этот запрос уже использован для другого вызова.",
                  "author_unavailable": "Переоткрой игру из меню бота."}
        raise DuelError(409, code, errors.get(code, "Не удалось создать вызов.")) from exc

    if duel["send_invite"]:
        try:
            message = await bot.send_message(
                recipient, f"⚔️ @{escape(user.username)} вызывает тебя на рок-дуэль!\n\n"
                "Угадай исполнителей, песни и героев рок-баек, расставь группы по году основания. До одиннадцати заданий.",
                reply_markup=duel_invite_kb(duel["id"]),
            )
        except (TelegramAPIError, asyncio.TimeoutError) as exc:
            logger.warning("Не удалось отправить приглашение в дуэль %s: %s", duel["id"], type(exc).__name__)
            await db.finish_duel_invite(duel["id"], error=type(exc).__name__)
        else:
            await db.finish_duel_invite(duel["id"], message_id=message.message_id)
    return await invite_response(await db.duel_invite_state(user.id, request_id), user.id)


async def cancel(request):
    try:
        status = await db.close_duel_challenge(int(request.match_info["id"]), request["duel_user"].id, "cancel")
    except ValueError:
        raise DuelError(404, "duel_not_found", "Этот вызов недоступен.")
    return response(status=status)


async def game_request(request):
    uid = request["duel_user"].id
    duel_id = int(request.match_info["id"])
    try:
        if request.method == "GET":
            return response(duel=await db.duel_game_state(duel_id, uid))
        rounds, artists = load_rounds()
        params = {}
        action = request.match_info["action"]
        if action in ("answer", "intro"):
            try:
                body = await request.json()
            except (ValueError, UnicodeError):
                body = None
            if not isinstance(body, dict):
                raise DuelError(400, "invalid_choice", "Выбери ответ из списка.")
        if action == "hint":
            params = {"hint": True}
        elif action == "intro":
            params = {"intro_seconds": body.get("seconds") or 0}
        elif action == "answer":
            types = {"order": list, "answer_key": str}
            fields = types.keys() & body.keys()
            if len(fields) != 1:
                raise DuelError(400, "invalid_choice", "Проверь выбранный ответ.")
            field = fields.pop()
            answer = body[field]
            if type(answer) is not types[field]:
                raise DuelError(400, "invalid_choice", "Проверь выбранный ответ.")
            rid = body.get("request_id")
            if not isinstance(rid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", rid):
                raise DuelError(400, "invalid_request_id", "Повтори ответ.")
            params = {"answer": answer, "request_id": rid}
        turn = await db.play_duel_task(duel_id, uid, int(request.match_info["task"]), rounds, artists, **params)
        return response(**turn)
    except ValueError as exc:
        errors = {"duel_not_found": (404, "Дуэль недоступна."), "wrong_task": (409, "Обнови задание."),
                  "invalid_choice": (400, "Выбери ответ из списка."), "already_tried": (409, "Этот ответ уже был."),
                  "request_conflict": (409, "Обнови задание.")}
        if str(exc) not in errors:
            raise
        status, message = errors[str(exc)]
        raise DuelError(status, str(exc), message) from exc


async def result_service(app):
    async def work():
        while True:
            try:
                pending = await db.claim_duel_results()
                if pending:
                    uid, game = pending
                    if game["status"] == "finished":
                        outcome = {"draw": "Ничья", "win": "Победа", "loss": "Поражение"}[game["outcome"]]
                        text = (f"🏁 Дуэль с @{escape(game['peer'])}: {outcome}. "
                                f"{game['own']['points']} : {game['opponent']['points']}.")
                        if game["decided_by"] == "time":
                            text += f" Решило время: {game['own']['seconds']} и {game['opponent']['seconds']} с."
                        if game["reason"] == "timeout":
                            text += " Срок игры истёк."
                    else:
                        text = "Дуэль отменена: " + ("никто не ответил." if game["reason"] == "no_answers"
                                                    else "задание недоступно.")
                    mid = error = None
                    try:
                        sent = await app[BOT].send_message(uid, text, reply_markup=duel_play_kb(
                            config.WEBAPP_URL, game["id"], result=True))
                        mid = sent.message_id
                    except (TelegramAPIError, asyncio.TimeoutError) as exc:
                        error = type(exc).__name__
                    await db.finish_duel_result_message(game["id"], uid, mid, error)
                    continue
            except Exception:
                logger.exception("Ошибка обслуживания дуэлей")
            await asyncio.sleep(15)

    task = asyncio.create_task(work())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def register_routes(app, bot=None):
    if bot is not None:
        app[BOT] = bot
        app.cleanup_ctx.append(result_service)
    app.middlewares.append(authorize)
    app.router.add_get(r"/duel/covers/{index:[0-9]{1,8}}", cover_image)
    app.router.add_get("/duel/intros/{track}", intro_audio)
    app.router.add_get("/duel/", page)
    app.router.add_get("/duel", page)
    app.router.add_get("/api/duel/bootstrap", bootstrap)
    app.router.add_get("/api/duel/rating", rating)
    app.router.add_post("/api/duel/challenges", challenge)
    app.router.add_post(r"/api/duel/challenges/{id:[0-9]{1,18}}/cancel", cancel)
    app.router.add_get(r"/api/duel/duels/{id:[0-9]{1,18}}", game_request)
    app.router.add_post(r"/api/duel/duels/{id:[0-9]{1,18}}/tasks/{task:[0-9]{1,18}}/{action:open|answer|intro|hint}", game_request)
