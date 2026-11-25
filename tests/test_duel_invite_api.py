"""Личный вызов через настоящее приложение и SQLite с тестовым Telegram."""
import asyncio
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from aiogram.enums import ChatMemberStatus
from aiogram.types import User

import config
import server
from database import db
from keyboards.kb import events_kb, main_menu_kb


def signed_header(user_id):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    secret = hmac.digest(b"WebAppData", b"test-token", hashlib.sha256)
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.digest(secret, check.encode(), hashlib.sha256).hex()
    return {"X-Telegram-Init-Data": urlencode(fields)}


class InviteApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(dir="."))
        self.enterContext(patch.object(db, "DB_PATH", str(Path(folder) / "test.db")))
        self.enterContext(patch.object(config, "BOT_TOKEN", "test-token"))
        await db.init_db()
        self.addAsyncCleanup(db.close_db)
        self.users = {}
        for uid, name in ((1, "Alice"), (2, "Boris")):
            self.users[uid] = User(id=uid, first_name=name, username=name, is_bot=False)
            await db.set_subscribed(uid, True, name)
        self.bot = AsyncMock()
        self.bot.get_chat_member.side_effect = lambda chat, uid: SimpleNamespace(
            user=self.users[uid], status=ChatMemberStatus.MEMBER,
        )
        self.bot.send_message.return_value = SimpleNamespace(message_id=123)
        self.client = TestClient(TestServer(server.create_app(self.bot)))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def invite(self, key="req1", nickname="@boris", uid=1):
        return await self.client.post("/api/duel/challenges", headers=signed_header(uid),
                                      json={"mode": "direct", "nickname": nickname, "request_id": key})

    async def test_menu_page_delivery_and_retry(self):
        for menu in (main_menu_kb(), events_kb(None)):
            self.assertFalse(any(b.web_app for row in menu.inline_keyboard for b in row))
        kb = events_kb("https://example.org/duel/")
        self.assertIn("/duel/", [b.web_app.url.removeprefix("https://example.org")
                                 for row in kb.inline_keyboard for b in row if b.web_app])
        page = await self.client.get("/duel/")
        self.assertIn("Бросить вызов", await page.text())
        first, second = await asyncio.gather(self.invite(), self.invite())
        states = [(await res.json())["duel"] for res in (first, second)]
        self.assertEqual(states[0]["id"], states[1]["id"])
        self.assertIn("sent", [s["delivery"] for s in states])
        self.bot.send_message.assert_awaited_once()
        recipient, text = self.bot.send_message.await_args.args
        self.assertEqual(recipient, 2)
        self.assertIn("@Alice", text)
        invite_id = states[0]["id"]
        state = await self.client.get("/api/duel/bootstrap", headers=signed_header(2))
        self.assertEqual((await state.json())["duel"]["role"], "opponent")
        denied = await self.client.post(f"/api/duel/challenges/{invite_id}/cancel", headers=signed_header(2))
        self.assertEqual(denied.status, 404)
        res = await self.client.post(f"/api/duel/challenges/{invite_id}/cancel", headers=signed_header(1))
        self.assertEqual((await res.json())["status"], "cancelled")
        self.assertEqual((await (await self.invite()).json())["duel"]["status"], "cancelled")
        self.bot.send_message.assert_awaited_once()

    async def test_no_success_before_telegram_acknowledges(self):
        started, acknowledge = asyncio.Event(), asyncio.Event()

        async def hold_send(*args, **kwargs):
            started.set()
            await acknowledge.wait()
            return SimpleNamespace(message_id=123)

        self.bot.send_message.side_effect = hold_send
        first = asyncio.create_task(self.invite())
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            self.assertFalse(first.done())
            replay = await self.invite()
            self.assertEqual((await replay.json())["duel"]["delivery"], "sending")
        finally:
            acknowledge.set()
            delivered = await first
        self.assertEqual((await delivered.json())["duel"]["delivery"], "sent")
        self.bot.send_message.assert_awaited_once()

    async def test_failed_delivery_releases_both_and_replay_does_not_resend(self):
        self.bot.send_message.side_effect = asyncio.TimeoutError()
        for _ in range(2):
            res = await self.invite()
            self.assertEqual((await res.json())["error"], "delivery_failed")
        self.bot.send_message.assert_awaited_once()
        self.bot.send_message.side_effect = None
        reverse = await self.invite("new-request", "alice", uid=2)
        self.assertEqual((await reverse.json())["duel"]["delivery"], "sent")

    async def test_restart_after_interrupted_send(self):
        await db.create_duel_challenge(1, "interrupted", opponent_nickname="boris", verified_recipient=2)
        await db.close_db()
        await db.init_db()
        self.assertEqual((await (await self.invite("interrupted")).json())["error"], "delivery_failed")
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await (await self.invite()).json())["duel"]["delivery"], "sent")

    async def test_invalid_identity_and_reassigned_nickname(self):
        denied = await self.client.post("/api/duel/challenges", json={})
        self.assertEqual(denied.status, 401)
        self.bot.get_chat_member.assert_not_awaited()
        self.users[2] = self.users[2].model_copy(update={"username": "Renamed"})
        self.assertEqual((await self.invite()).status, 404)
        self.bot.send_message.assert_not_awaited()

    async def accept_game(self):
        from handlers.duel import accept
        with patch.object(config, "WEBAPP_URL", "https://example.org"):
            invite = (await (await self.invite()).json())["duel"]
            keyboard = self.bot.send_message.await_args.kwargs["reply_markup"]
            data = keyboard.inline_keyboard[0][0].callback_data
            self.assertEqual(data, f"duel:a:{invite['id']}")
            cb = SimpleNamespace(from_user=self.users[2], bot=self.bot, data=data,
                                 message=AsyncMock(), answer=AsyncMock())
            await accept(cb)
            await accept(cb)
            self.assertEqual(self.bot.send_message.await_count, 2)
            self.assertIn(f"?id={invite['id']}", cb.message.edit_text.await_args.kwargs[
                "reply_markup"].inline_keyboard[0][0].web_app.url)
            return invite["id"]

    async def test_two_players_play_all_five_rounds(self):
        from duel_content import load_rounds
        duel_id = await self.accept_game()
        cards, _ = load_rounds()
        path = f"/api/duel/duels/{duel_id}"

        async def state(uid):
            res = await self.client.get(path, headers=signed_header(uid))
            return (await res.json())["duel"]

        async def turn(uid, task_id, action, **body):
            res = await self.client.post(f"{path}/tasks/{task_id}/{action}", headers=signed_header(uid), json=body)
            return await res.json()

        first = (await state(1))["next_task"]
        lineup = {}
        quartets = {}
        songs = {}
        stories = {}
        for uid in (1, 2):
            self.assertEqual((await state(uid))["count"], 11)
            for position in range(1, 5):
                game = await state(uid)
                self.assertNotIn("opponent", game)
                task_id = game["next_task"]
                if position == 1:
                    self.assertEqual(task_id, first)
                opened = await turn(uid, task_id, "open")
                task = opened["task"]
                self.assertNotIn("solution", task)
                self.assertNotIn("album", task)
                self.assertNotIn("artist", task)
                if position <= 2:
                    self.assertEqual(task["round"], "detective")
                    if position == 1 and uid == 1:
                        invalid = await turn(uid, task_id, "answer", request_id="order", order=list(cards["detective"])[:4])
                        self.assertEqual(invalid["error"], "invalid_choice")
                    self.assertEqual(len(task["facts"]), 5)
                    card = next(c for c in cards["detective"].values() if c["facts"] == task["facts"])
                else:
                    self.assertEqual(task["round"], "cover")
                    self.assertRegex(task["image"], r"^/duel/covers/[0-9]+$")
                    filename, card = next((k, c) for k, c in cards["cover"].items() if c["image"] == task["image"])
                    image = await self.client.get(task["image"])
                    self.assertEqual(image.status, 200)
                    self.assertEqual(await image.read(), (config.CONTENT_DIR / "duel_covers" / filename).read_bytes())
                key = card["artist"]
                if uid == 1:
                    self.assertNotIn(key, lineup.values())
                    lineup[task_id] = key
                else:
                    self.assertEqual(lineup[task_id], key)
                wrong = next(k for k in cards["detective"] if k != key)
                if uid == 2 and position == 1:
                    bad = dict(request_id="wrong", answer_key=wrong)
                    mistake = await turn(uid, task_id, "answer", **bad)
                    self.assertIsNone(mistake["feedback"]["points"])
                    self.assertNotIn("solution", mistake["feedback"])
                    self.assertNotIn("album", mistake["feedback"])
                    await db.close_db()
                    await db.init_db()
                    resume = (await turn(uid, task_id, "open"))["task"]
                    self.assertEqual(resume["shown_at"], task["shown_at"])
                    self.assertEqual(resume["wrong"], [wrong])
                    retry = await turn(uid, task_id, "answer", **bad)
                    self.assertEqual(retry["feedback"], mistake["feedback"])
                    conflict = await turn(uid, task_id, "answer", request_id="wrong", answer_key=key)
                    self.assertEqual(conflict["error"], "request_conflict")
                    self.assertEqual((await turn(uid, task_id, "hint"))["error"], "wrong_task")
                saved = await turn(uid, task_id, "answer", request_id="right", answer_key=key)
                if uid == 2 and position == 1:
                    self.assertEqual(saved["feedback"]["points"], 2)
                self.assertEqual(saved["feedback"]["solution"], key)
                if position > 2:
                    self.assertEqual(saved["feedback"]["album"], card["album"])
            used = set(lineup.values())
            for position in (5, 6):
                task_id = (await state(uid))["next_task"]
                opened = await turn(uid, task_id, "open")
                task = opened["task"]
                self.assertEqual((task["round"], task["position"]), ("chronology", position))
                self.assertNotIn("year", json.dumps(opened))
                order = [a["key"] for a in opened["artists"]]
                self.assertEqual(len(set(order)), 4)
                self.assertTrue(used.isdisjoint(order))
                used.update(order)
                if uid == 1:
                    quartets[task_id] = order
                else:
                    self.assertEqual(order, quartets[task_id])
                years = cards["chronology"]["years"]
                correct = sorted(order, key=years.get)
                self.assertNotEqual(order, correct)
                self.assertTrue(all(years[b] - years[a] >= cards["chronology"]["min_gap"]
                                    for a, b in zip(correct, correct[1:])))
                if position == 5 and uid == 1:
                    for choice in ({"order": order[:3]}, {"order": order[:3] + order[:1]},
                                   {"order": order[:3] + ["unknown"]}, {"answer_key": order[0]},
                                   {"answer_key": order[0], "order": order}):
                        rejected = await turn(uid, task_id, "answer", request_id="check", **choice)
                        self.assertEqual(rejected["error"], "invalid_choice")
                    await db.close_db()
                    await db.init_db()
                    resumed = await turn(uid, task_id, "open")
                    self.assertEqual((resumed["task"], resumed["artists"]), (task, opened["artists"]))
                if uid == 1:
                    guess, expected = correct, 4
                elif position == 5:
                    guess, expected = [correct[1], correct[0], *correct[2:]], 2
                else:
                    guess, expected = correct[::-1], 0
                saved = await turn(uid, task_id, "answer", request_id="check", order=guess)
                self.assertEqual(saved["feedback"]["points"], expected)
                self.assertEqual(saved["feedback"]["chronology"], [{"key": k, "year": years[k]} for k in correct])
                replay = await turn(uid, task_id, "answer", request_id="check", order=guess)
                self.assertEqual(replay["feedback"], saved["feedback"])
                conflict = await turn(uid, task_id, "answer", request_id="check", order=guess[::-1])
                self.assertEqual(conflict["error"], "request_conflict")
            for position in (7, 8, 9):
                task_id = (await state(uid))["next_task"]
                task = (await turn(uid, task_id, "open"))["task"]
                self.assertEqual((task["round"], task["position"], task["seconds"]), ("intro", position, 5))
                self.assertNotIn("correct", task)
                self.assertNotIn("options", task)
                self.assertEqual(set(task["songs"]), {c["song"] for c in cards["intro"].values()})
                card = cards["intro"][task["audio"].split("/")[-1]]
                self.assertNotIn(card["artist"], used)
                used.add(card["artist"])
                if uid == 1:
                    songs[task_id] = (task["audio"], task["songs"])
                    clip = await self.client.get(task["audio"])
                    self.assertEqual(clip.content_type, "audio/mpeg")
                    self.assertGreater(len(await clip.read()), 1000)
                else:
                    self.assertEqual(songs[task_id], (task["audio"], task["songs"]))
                for bad in (-1, 4, True, "0"):
                    rejected = await turn(uid, task_id, "answer", option_id=bad, request_id="guess")
                    self.assertEqual(rejected["error"], "invalid_choice")
                correct = card["song"]
                if uid == 2 and position == 7:
                    for requested, expected in [(10, 10), (15, 15), (10, 15)]:
                        self.assertEqual((await turn(uid, task_id, "intro", seconds=requested))["seconds"], expected)
                    await db.close_db()
                    await db.init_db()
                    resumed = (await turn(uid, task_id, "open"))["task"]
                    self.assertEqual((resumed["shown_at"], resumed["seconds"]), (task["shown_at"], 15))
                elif uid == 1 and position == 8:
                    self.assertEqual((await turn(uid, task_id, "intro", seconds=10))["seconds"], 10)
                guess = next(s for s in task["songs"] if s != correct) if uid == 2 and position == 8 else correct
                answer = dict(answer_key=guess, request_id="guess")
                saved = await turn(uid, task_id, "answer", **answer)
                self.assertEqual(saved["feedback"], {"song": correct,
                                 "points": {(1, 7): 3, (1, 8): 2, (2, 7): 1, (2, 8): 0, (1, 9): 3, (2, 9): 3}[uid, position]})
                self.assertEqual((await turn(uid, task_id, "answer", **answer))["feedback"], saved["feedback"])
            for position in (10, 11):
                task_id = (await state(uid))["next_task"]
                opened = await turn(uid, task_id, "open")
                task = opened["task"]
                self.assertEqual((task["round"], task["position"]), ("tale", position))
                self.assertNotIn("reveal", task)
                self.assertNotIn("solution", task)
                card = next(c for c in cards["tale"].values() if c["text"] == task["text"])
                self.assertNotIn(card["artist"], used)
                used.add(card["artist"])
                options = {a["key"] for a in opened["artists"]}
                self.assertEqual(options, card["choices"].keys())
                if uid == 1:
                    stories[task_id] = task["text"]
                    guess = card["solution"]
                else:
                    self.assertEqual(task["text"], stories[task_id])
                    guess = next(iter(options - card["accepted"]))
                expected = 3 if uid == 1 else 0
                if uid == 2:
                    hint = await turn(uid, task_id, "hint")
                    variants = [a["key"] for a in hint["artists"]]
                    self.assertTrue(hint["task"]["hinted"])
                    self.assertEqual(len(set(variants)), 4)
                    self.assertEqual(card["accepted"].intersection(variants), {card["solution"]})
                    denied = next(iter(options - set(variants)))
                    rejected = await turn(uid, task_id, "answer", request_id="outside", answer_key=denied)
                    self.assertEqual(rejected["error"], "invalid_choice")
                    await db.close_db()
                    await db.init_db()
                    self.assertEqual((await turn(uid, task_id, "open"))["artists"], hint["artists"])
                    expected = 1 if position == 10 else 0
                    guess = card["solution"] if expected else next(k for k in variants if k != card["solution"])
                attempt = dict(answer_key=guess, request_id="story")
                saved = await turn(uid, task_id, "answer", **attempt)
                self.assertEqual(saved["feedback"], {"points": expected,
                                 "solution": card["solution"], "reveal": card["reveal"]})
                self.assertEqual((await turn(uid, task_id, "answer", **attempt))["feedback"], saved["feedback"])
                if position == 10:
                    repeat = await turn(uid, task_id, "answer", request_id="again", answer_key=card["solution"])
                    self.assertEqual(repeat["error"], "wrong_task")
            if uid == 1:
                self.assertIsNone((await state(1))["next_task"])
                self.assertNotIn("opponent", await state(1))
        final = await state(1)
        self.assertEqual((final["own"]["points"], final["opponent"]["points"], final["outcome"]), (34, 18, "win"))
        self.assertEqual((await state(2))["outcome"], "loss")
        for uid, expected in ((1, 1510), (2, 1490)):
            rating = await self.client.get("/api/duel/rating", headers=signed_header(uid))
            data = await rating.json()
            self.assertEqual((data["me"]["rating"], data["me"]["played"]), (expected, 1))
            self.assertEqual(data["rows"], [])
        self.assertIsNone(await db.duel_invite_state(1))
        self.assertEqual((await self.invite(key="again")).status, 200)

    async def test_game_access_deadline_and_missing_content(self):
        from duel_content import load_rounds
        duel_id = await self.accept_game()
        cards, artists = load_rounds()
        state = await db.duel_game_state(duel_id, 1)
        task = state["next_task"]
        with self.assertRaisesRegex(ValueError, "duel_not_found"):
            await db.play_duel_task(duel_id, 3, task, cards, artists)
        with self.assertRaisesRegex(ValueError, "wrong_task"):
            await db.play_duel_task(duel_id, 1, task, cards, artists, answer="queen", request_id="1")
        await db.play_duel_task(duel_id, 1, task, cards, artists)
        with patch.object(db.time, "time_ns", return_value=(time.time_ns() + 86_401_000_000_000)):
            late = await db.play_duel_task(duel_id, 1, task, cards, artists, answer="queen", request_id="2")
        self.assertEqual(late["duel"]["reason"], "no_answers")
        invite = await db.create_duel_challenge(1, "next", opponent_nickname="boris")
        await db.accept_duel(invite["id"], 2, cards)
        task = (await db.duel_game_state(invite["id"], 1))["next_task"]
        cancelled = await db.play_duel_task(invite["id"], 1, task, {}, artists)
        self.assertEqual(cancelled["duel"]["reason"], "content_unavailable")
        self.assertIsNone(await db.duel_invite_state(2))

    async def test_three_misses_and_timeout_result_notifications(self):
        from duel_content import load_rounds
        cards, artists = load_rounds()
        duel_id = await self.accept_game()
        game = await db.duel_game_state(duel_id, 2)
        task = game["next_task"]
        opened = (await db.play_duel_task(duel_id, 2, task, cards, artists))["task"]
        key = next(k for k, c in cards["detective"].items() if opened["facts"] == c["facts"])
        guesses = [k for k in artists if k != key][:3]
        for i, guess in enumerate(guesses):
            response = await db.play_duel_task(duel_id, 2, task, cards, artists, answer=guess, request_id=str(i))
        self.assertEqual(response["feedback"]["points"], 0)
        self.assertEqual(response["feedback"]["solution"], key)
        self.assertNotEqual(response["duel"]["next_task"], task)
        with patch.object(db.time, "time_ns", return_value=time.time_ns() + 86_401_000_000_000):
            results = [await db.claim_duel_results(), await db.claim_duel_results()]
            self.assertIsNone(await db.claim_duel_results())
        self.assertEqual({uid for uid, _ in results}, {1, 2})
        for uid, result in results:
            self.assertEqual(result["reason"], "timeout")
            self.assertEqual(result["outcome"], "win" if uid == 2 else "loss")

    async def test_cover_ends_after_first_miss(self):
        from duel_content import load_rounds
        rounds, artists = load_rounds()
        invite = await db.create_duel_challenge(1, "covers", opponent_nickname="boris")
        duel_id = invite["id"]
        await db.accept_duel(duel_id, 2, {"cover": rounds["cover"]})
        game = await db.duel_game_state(duel_id, 1)
        self.assertEqual(game["count"], 2)
        task_id = game["next_task"]
        opened = await db.play_duel_task(duel_id, 1, task_id, rounds, artists)
        cover = next(c for c in rounds["cover"].values() if c["image"] == opened["task"]["image"])
        wrong = next(k for k in artists if k != cover["artist"])
        reply = await db.play_duel_task(duel_id, 1, task_id, rounds, artists, answer=wrong, request_id="miss")
        with self.assertRaisesRegex(ValueError, "wrong_task"):
            await db.play_duel_task(duel_id, 1, task_id, rounds, artists, answer=cover["artist"], request_id="retry")
        self.assertEqual(reply["feedback"]["album"], cover["album"])
        self.assertEqual(reply["feedback"]["points"], 0)
        self.assertNotEqual(game["next_task"], reply["duel"]["next_task"])

    async def test_short_cover_pool_preserves_two_task_game(self):
        from duel_content import load_rounds
        rounds, artists = load_rounds()
        one_artist = next(iter(rounds["cover"].values()))["artist"]
        rounds["cover"] = {k: c for k, c in rounds["cover"].items() if c["artist"] == one_artist}
        rounds["chronology"]["years"] = dict.fromkeys(artists, 1970)
        rounds["intro"] = dict(list(rounds["intro"].items())[:2])
        rounds["tale"] = dict(list(rounds["tale"].items())[:1])
        invite = await db.create_duel_challenge(1, "short", opponent_nickname="boris")
        await db.accept_duel(invite["id"], 2, rounds)
        # После пополнения каталога уже принятые игры не удлиняются.
        rounds, artists = load_rounds()
        for _ in range(2):
            game = await db.duel_game_state(invite["id"], 1)
            self.assertEqual(game["count"], 2)
            task_id = game["next_task"]
            opened = await db.play_duel_task(invite["id"], 1, task_id, rounds, artists)
            task = opened["task"]
            key = next(k for k, c in rounds["detective"].items() if c["facts"] == task["facts"])
            await db.play_duel_task(invite["id"], 1, task_id, rounds, artists, answer=key, request_id="right")
        self.assertIsNone((await db.duel_game_state(invite["id"], 1))["next_task"])
