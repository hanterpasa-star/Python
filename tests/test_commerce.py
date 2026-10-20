from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from shoe_shop_bot.commerce import date_bounds, money, parse_money
from shoe_shop_bot.storage import Actor, Conflict, NoChange, Store


class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.store = Store(self.path)
        self.actor = Actor(101, "Сотрудник")
        self.a = self.store.create("Белые NIKE", "photo-a", {"38": 3, "39": 2}, self.actor)
        self.b = self.store.create("Чёрные Adidas", "photo-b", {"40": 1}, self.actor)

    def item(self, product=None, size="38", quantity=1, price=150050):
        product = product or self.a
        return dict(product_id=product.id, version=product.version, size=size, quantity=quantity, unit_price=price)

    def report(self):
        return self.store.report("2000", "2100")

    def test_exact_money_validation_and_formatting(self):
        self.assertEqual(parse_money("1500,05"), 150005)
        self.assertEqual(parse_money("0.01"), 1)
        self.assertEqual(money(-123456), "−1 234,56 грн")
        for invalid in ["0", "-5", "1.234", "NaN", "1e5", "10000001", "1 500"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_money(invalid)

    def test_atomic_multiple_items_and_idempotent_confirmation(self):
        items = [self.item(quantity=2), self.item(size="39"), self.item(self.b, "40", price=12345)]
        sale = self.store.sell(items, self.actor, "sale-nonce", [-123])
        self.assertEqual(sale["total"], 462495)
        self.assertEqual(self.store.get(self.a.id).stock, {"38": 1, "39": 1})
        self.assertEqual(self.store.get(self.b.id).stock, {})
        replay = self.store.sell(items, self.actor, "sale-nonce", [-123])
        self.assertEqual(replay["id"], sale["id"])
        self.assertEqual(self.report()["sold"], 4)
        self.assertEqual(self.report()["net_amount"], 462495)
        self.assertEqual(self.store.delivery_counts(), {"pending": 2})
        events = self.store.history()
        self.assertCountEqual(events[1]["low_stock"], ["38", "39"])
        self.assertEqual(events[0]["low_stock"], ["40"])

    def test_second_item_failure_rolls_back_entire_sale_and_notifications(self):
        for items in [[self.item(), self.item(self.b, "40", quantity=2)],
                      [self.item(quantity=2), self.item(quantity=2)]]:
            with self.assertRaises(ValueError):
                self.store.sell(items, self.actor, "bad", [-123])
            self.assertEqual(self.store.get(self.a.id).version, 1)
            self.assertEqual(self.store.sales_page()[1], 0)
            self.assertEqual(self.store.delivery_counts(), {})

    def test_stale_cart_is_atomic_even_when_stock_still_available(self):
        self.store.change(self.b.id, 1, "add_sizes", {"41": 1}, self.actor)
        with self.assertRaises(Conflict):
            self.store.sell([self.item(), self.item(self.b, "40")], self.actor, "stale")
        self.assertEqual(self.store.get(self.a.id).version, 1)
        self.assertEqual(self.report()["sold"], 0)

    def test_concurrent_sales_cannot_sell_same_pair(self):
        def attempt(nonce):
            try:
                Store(self.path).sell([self.item(self.b, "40")], self.actor, nonce)
                return "saved"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(list(executor.map(attempt, ["one", "two"])), ["saved", "conflict"])
        self.assertEqual(self.report()["sold"], 1)

    def test_partial_return_then_void_restores_only_remaining_pairs(self):
        sale = self.store.sell([self.item(quantity=3)], self.actor, "original", stamp="2026-10-01T10:00:00+00:00")
        item_id = sale["items"][0]["id"]
        self.store.return_item(sale["id"], item_id, 1, self.actor, "return", stamp="2026-10-02T10:00:00+00:00")
        self.store.return_item(sale["id"], item_id, 1, self.actor, "return")
        self.assertEqual(self.store.get(self.a.id).stock["38"], 1)
        self.store.void_sale(sale["id"], self.actor, stamp="2026-10-03T10:00:00+00:00")
        self.store.void_sale(sale["id"], self.actor)
        self.assertEqual(self.store.get(self.a.id).stock, self.a.stock)
        report = self.report()
        self.assertEqual((report["sold"], report["returned"], report["voided"], report["net_amount"]), (3, 1, 2, 0))
        with self.assertRaises(NoChange):
            self.store.return_item(sale["id"], item_id, 1, self.actor, "after-void")
        day = self.store.report(*date_bounds(date(2026, 10, 2), date(2026, 10, 2)))
        self.assertEqual(day["net_amount"], -150050)

    def test_over_return_and_return_of_another_sale_line_are_rejected(self):
        sale = self.store.sell([self.item()], self.actor, "s")
        for item_id, quantity in [(sale["items"][0]["id"], 2), (9999, 1)]:
            with self.assertRaises(NoChange):
                self.store.return_item(sale["id"], item_id, quantity, self.actor, f"r-{item_id}")
        self.assertEqual(self.report()["returned"], 0)

    def test_return_reactivates_archived_product_and_keeps_original_price(self):
        sale = self.store.sell([self.item(price=101)], self.actor, "s")
        self.store.change(self.a.id, 2, "archive", None, self.actor)
        self.store.return_item(sale["id"], sale["items"][0]["id"], 1, self.actor, "r")
        self.assertEqual(self.store.get(self.a.id).stock, self.a.stock)
        self.assertEqual(self.report()["deductions"], 101)

    def test_void_restore_failure_rolls_back_all_positions(self):
        sale = self.store.sell([self.item(), self.item(self.b, "40")], self.actor, "s")
        self.store.change(self.b.id, 2, "set_sizes", {"40": 9999}, self.actor)
        before = self.store.get(self.a.id)
        with self.assertRaises(ValueError):
            self.store.void_sale(sale["id"], self.actor)
        self.assertEqual(self.store.get(self.a.id), before)
        self.assertEqual(self.store.sale(sale["id"])["status"], "sold")
        self.assertEqual(self.report()["voided"], 0)

    def test_unicode_search_categories_and_summary(self):
        self.assertEqual(self.store.page(query="БЕЛЫЕ")[0][0].id, self.a.id)
        self.assertEqual(self.store.page(query="38")[1], 1)
        self.assertEqual(self.store.page(query=f"#{self.b.id}")[0][0].id, self.b.id)
        self.store.change(self.a.id, 1, "category", "Nike", self.actor)
        self.assertEqual(self.store.page(category="Nike")[1], 1)
        self.assertEqual(self.store.summary()["pairs"], 6)
        self.assertEqual(self.store.duplicates("  БЕЛЫЕ   nike ")[0].id, self.a.id)

    def test_previous_database_with_obsolete_column_keeps_all_data(self):
        # Earlier releases stored codes; upgrading must never discard the database.
        self.store.grant(202)
        with self.store.connect() as db:
            db.execute("ALTER TABLE products ADD COLUMN code TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE products SET code='4820012345678' WHERE id=?", (self.a.id,))
        reopened = Store(self.path)
        self.assertEqual(reopened.get(self.a.id), self.a)
        reopened.change(self.a.id, 1, "rename", "Новое название", self.actor)
        sale = reopened.sell([self.item(reopened.get(self.a.id))], self.actor, "after-upgrade")
        self.assertEqual(sale["total"], 150050)
        self.assertTrue(reopened.is_staff(202))
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT code FROM products WHERE id=?", (self.a.id,)).fetchone()[0], "4820012345678")

    def test_kyiv_day_bounds_handle_dst_and_midnight(self):
        for day, expected in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]:
            start, end = map(datetime.fromisoformat, date_bounds(day, day))
            self.assertEqual((end - start).total_seconds() / 3600, expected)
        self.store.sell([self.item()], self.actor, "late", stamp="2026-10-19T21:30:00+00:00")
        self.assertEqual(self.store.report(*date_bounds(date(2026, 10, 20), date(2026, 10, 20)))["sold"], 1)
        self.assertEqual(self.store.report(*date_bounds(date(2026, 10, 19), date(2026, 10, 19)))["sold"], 0)

    def test_real_legacy_schema_is_backed_up_and_migrated_without_losing_data(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript("""
                CREATE TABLE products(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,photo TEXT NOT NULL,
                    stock TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,archived INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE staff(user_id INTEGER PRIMARY KEY,added_at TEXT NOT NULL);
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO products VALUES(7,'Старая модель','old-photo','{"38": 2}',4,0);
                INSERT INTO staff VALUES(202,'2026-10-01T10:00:00+00:00');
                INSERT INTO settings VALUES('notification_chat','-123');
            """)
        migrated = Store(legacy)
        self.assertEqual(migrated.get(7).photo, "old-photo")
        self.assertEqual(migrated.get(7).stock, {"38": 2})
        self.assertEqual(migrated.get(7).version, 4)
        self.assertTrue(migrated.is_staff(202))
        self.assertEqual(migrated.notification_chat(), -123)
        copies = list((legacy.parent / "backups").glob("before-upgrade-*.sqlite3"))
        self.assertEqual(len(copies), 1)
        with closing(sqlite3.connect(copies[0])) as db:
            self.assertNotIn("category", [r[1] for r in db.execute("PRAGMA table_info(products)")])
        Store(legacy)
        self.assertEqual(len(list((legacy.parent / "backups").glob("before-upgrade-*.sqlite3"))), 1)
