from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from shoe_shop_bot.storage import Actor, Conflict, NoChange, NotFound, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.store = Store(self.path)
        self.actor = Actor(101, "Владелец")

    def create(self, stock=None, targets=()):
        return self.store.create("Белые кроссовки", "telegram-photo", {"38": 2} if stock is None else stock, self.actor, targets)

    def test_persistence_including_photo_staff_and_group(self):
        product = self.create()
        self.store.grant(202)
        self.store.set_notification_chat(-123)
        reopened = Store(self.path)
        self.assertEqual(reopened.get(product.id), product)
        self.assertTrue(reopened.is_staff(202))
        self.assertEqual(reopened.notification_chat(), -123)

    def test_in_stock_list_excludes_empty_and_archived_but_edit_includes_empty(self):
        available = self.create()
        empty = self.create({})
        deleted = self.create()
        self.store.change(deleted.id, deleted.version, "archive", None, self.actor)
        self.assertEqual(self.store.page()[0], [available])
        self.assertEqual(self.store.page(include_empty=True)[0], [empty, available])
        with self.assertRaises(NotFound):
            self.store.get(deleted.id)

    def test_pagination(self):
        for _ in range(8):
            self.create()
        first, total = self.store.page(limit=6)
        second, _ = self.store.page(limit=6, offset=6)
        self.assertEqual((len(first), len(second), total), (6, 2, 8))
        self.assertFalse({p.id for p in first} & {p.id for p in second})

    def test_stale_edit_cannot_overwrite_new_stock_or_enqueue_notification(self):
        product = self.create(targets=[-123])
        updated = self.store.change(product.id, 1, "add_sizes", {"39": 1}, self.actor, [-123])
        with self.assertRaises(Conflict):
            self.store.change(product.id, 1, "set_sizes", {"40": 1}, Actor(202, "Другой"), [-123])
        self.assertEqual(self.store.get(product.id), updated)
        self.assertEqual(len(self.store.history()), 2)
        self.assertEqual(self.store.delivery_counts(), {"pending": 2})

    def test_two_simultaneous_editors_exactly_one_succeeds(self):
        product = self.create()

        def change(size):
            try:
                Store(self.path).change(product.id, 1, "add_sizes", {size: 1}, self.actor)
                return "saved"
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(change, ["39", "40"]))
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        self.assertEqual(self.store.get(product.id).version, 2)

    def test_no_op_and_invalid_edit_do_not_change_revision_or_history(self):
        product = self.create()
        with self.assertRaises(NoChange):
            self.store.change(product.id, 1, "remove_sizes", ["40"], self.actor)
        with self.assertRaises(ValueError):
            self.store.change(product.id, 1, "add_sizes", {"39": -1}, self.actor)
        self.assertEqual(self.store.get(product.id), product)
        self.assertEqual(len(self.store.history()), 1)

    def test_event_keeps_snapshot_and_actor(self):
        product = self.create(targets=[-123, -123])
        self.store.change(product.id, 1, "rename", "Новое имя", self.actor, [-123])
        oldest = self.store.pending()[0]
        self.assertEqual(oldest["payload"]["title"], "Белые кроссовки")
        newest = self.store.history()[0]
        self.assertEqual(newest["before_title"], "Белые кроссовки")
        self.assertEqual(newest["actor_id"], 101)
        self.assertEqual(self.store.delivery_counts(), {"pending": 2})

    def test_staff_revocation_cancels_queued_private_notifications(self):
        self.store.grant(202)
        self.store.grant(202)
        self.assertEqual(self.store.staff_ids(), [202])
        self.create(targets=[202, -123])
        self.store.revoke(202)
        self.assertFalse(self.store.is_staff(202))
        self.assertEqual(self.store.delivery_counts(), {"cancelled": 1, "pending": 1})

    def test_online_backup_includes_committed_wal_and_does_not_overwrite(self):
        product = self.create()
        self.store.grant(202)
        backup = Path(self.temp.name) / "backup.sqlite3"
        self.store.backup(backup)
        restored = Store(backup)
        self.assertEqual(restored.get(product.id), product)
        self.assertTrue(restored.is_staff(202))
        with self.assertRaises(ValueError):
            self.store.backup(backup)
        with self.assertRaises(ValueError):
            self.store.backup(self.path)

    def test_pending_preserves_event_order_per_chat(self):
        product = self.create(targets=[-123])
        self.store.change(product.id, 1, "add_sizes", {"39": 1}, self.actor, [-123])
        first = self.store.pending()
        self.assertEqual(len(first), 1)
        self.store.delivery_result(first[0]["event_id"], -123, status="pending", delay=600)
        self.assertEqual(self.store.pending(), [])
        self.store.delivery_result(first[0]["event_id"], -123)
        self.assertEqual(self.store.pending()[0]["payload"]["action"], "add_sizes")
