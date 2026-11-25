"""Проверки жизненного цикла приглашения на временной БД."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import db


class ChallengeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(dir="."))
        self.enterContext(patch.object(db, "DB_PATH", str(Path(folder) / "test.db")))
        await db.init_db()
        self.addAsyncCleanup(db.close_db)
        for user_id, nickname in enumerate(("Author", "Rival", "Other"), 1):
            await db.set_subscribed(user_id, True, nickname)

    async def test_direct_retries_and_access(self):
        create = db.create_duel_challenge
        a, b = await asyncio.gather(create(1, "one", opponent_nickname="@Rival"),
                                    create(1, "one", opponent_nickname="rival"))
        self.assertEqual(a["id"], b["id"])
        with self.assertRaisesRegex(ValueError, "request_conflict"):
            await create(1, "one")
        with self.assertRaisesRegex(ValueError, "duel_not_found"):
            await db.close_duel_challenge(a["id"], 2, "cancel")
        with self.assertRaisesRegex(ValueError, "duel_not_found"):
            await db.close_duel_challenge(a["id"], 3, "decline")
        self.assertEqual(await db.close_duel_challenge(a["id"], 2, "decline"), "declined")
        self.assertEqual(await db.close_duel_challenge(a["id"], 1, "cancel"), "declined")
        self.assertEqual((await create(1, "one", opponent_nickname="Rival"))["status"], "declined")
        self.assertEqual((await create(2, "two", opponent_nickname="Author"))["status"], "pending")

    async def test_competing_invites_and_unrelated_write(self):
        results = await asyncio.gather(
            db.create_duel_challenge(1, "first", opponent_nickname="Rival"),
            db.create_duel_challenge(3, "second", opponent_nickname="Rival"),
            db.save_rockle_result(2, "2026-11-22", 15), return_exceptions=True,
        )
        winners = [r for r in results[:2] if isinstance(r, dict)]
        self.assertEqual(len(winners), 1)
        self.assertIn("player_busy", [str(r) for r in results[:2]])
        self.assertEqual(results[2], 15)
        other = 3 if winners[0]["author_id"] == 1 else 1
        # Откат освободил место автора неудавшегося вызова.
        open_duel = await db.create_duel_challenge(other, "open")
        self.assertEqual(open_duel["mode"], "open")
        self.assertEqual(await db.close_duel_challenge(open_duel["id"], other, "cancel"), "cancelled")
        async with db._db.execute("SELECT COUNT(*) FROM duels") as cur:
            self.assertEqual((await cur.fetchone())[0], 2)

    async def test_deadline_and_restart(self):
        with patch.object(db.time, "time_ns", return_value=1_000_000_000):
            invite = await db.create_duel_challenge(1, "one", opponent_nickname="Rival")
            await db.close_db()
            await db.init_db()
            with self.assertRaisesRegex(ValueError, "player_busy"):
                await db.create_duel_challenge(2, "two")
        boundary = (1000 + 24 * 3600 * 1000) * 1_000_000
        with patch.object(db.time, "time_ns", return_value=boundary):
            self.assertEqual(await db.close_duel_challenge(invite["id"], 1, "cancel"), "expired")
            await db.create_duel_challenge(2, "two", opponent_nickname="Author")
        with patch.object(db.time, "time_ns", return_value=2 * boundary):
            await db.close_db()
            await db.init_db()
            self.assertEqual((await db.create_duel_challenge(2, "two", opponent_nickname="Author"))["status"],
                             "expired")
            self.assertEqual((await db.create_duel_challenge(1, "three"))["status"], "pending")
