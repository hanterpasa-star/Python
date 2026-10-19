from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from telegram.error import Forbidden, NetworkError, RetryAfter

from shoe_shop_bot.notifications import deliver_once
from shoe_shop_bot.storage import Actor, Store


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "shop.sqlite3")
        self.store.create("Товар", "photo-file", {"38": 2}, Actor(101, "Владелец"), [-123])
        self.bot = AsyncMock()

    async def test_photo_and_sizes_are_delivered_and_not_resent(self):
        await deliver_once(self.bot, self.store, 101)
        self.bot.send_photo.assert_awaited_once()
        args = self.bot.send_photo.await_args.kwargs
        self.assertEqual(args["chat_id"], -123)
        self.assertEqual(args["photo"], "photo-file")
        self.assertIn("38 × 2", args["caption"])
        self.assertIn("Владелец", args["caption"])
        await deliver_once(self.bot, self.store, 101)
        self.assertEqual(self.bot.send_photo.await_count, 1)
        self.assertEqual(self.store.delivery_counts(), {"sent": 1})

    async def test_network_failure_survives_restart_and_retries_later(self):
        self.bot.send_photo.side_effect = NetworkError("offline")
        await deliver_once(self.bot, self.store, 101)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.delivery_counts(), {"pending": 1})
        self.assertEqual(reopened.pending(), [])
        with reopened.connect() as db:
            db.execute("UPDATE deliveries SET next_attempt=0")
        self.bot.send_photo.side_effect = None
        await deliver_once(self.bot, reopened, 101)
        self.assertEqual(reopened.delivery_counts(), {"sent": 1})

    async def test_blocked_bot_is_reported_as_failed(self):
        self.bot.send_photo.side_effect = Forbidden("blocked")
        await deliver_once(self.bot, self.store, 101)
        self.assertEqual(self.store.delivery_counts(), {"failed": 1})

    async def test_telegram_rate_limit_leaves_delivery_pending(self):
        self.bot.send_photo.side_effect = RetryAfter(10)
        await deliver_once(self.bot, self.store, 101)
        self.assertEqual(self.store.delivery_counts(), {"pending": 1})
        self.assertEqual(self.store.pending(), [])

    async def test_notification_is_not_sent_to_a_non_staff_user(self):
        self.store.create("Ещё товар", "photo", {"39": 1}, Actor(101, "Владелец"), [999])
        await deliver_once(self.bot, self.store, 101)
        self.assertEqual(self.store.delivery_counts(), {"sent": 1, "cancelled": 1})
        self.assertEqual(self.bot.send_photo.await_count, 1)
