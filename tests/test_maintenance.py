from datetime import datetime, timedelta, timezone
from pathlib import Path
from io import BytesIO
import tempfile
import unittest
from PIL import Image
import zxingcpp

from shoe_shop_bot.codes import decode_image, product_reference, qr_image
from shoe_shop_bot.maintenance import maintenance_tick, make_backup, schedule_reports
from shoe_shop_bot.process import AlreadyRunning, instance_lock
from shoe_shop_bot.storage import Actor, NoChange, Store


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "shop.sqlite3")
        self.product = self.store.create("Модель", "photo", {"38": 2}, Actor(101, "Owner"))

    def test_qr_round_trip_and_input_validation(self):
        image = qr_image(self.product.id, "test_shop_bot")
        text = decode_image(image.read())[0]
        self.assertEqual(product_reference(text, "test_shop_bot"), ("id", "1"))
        self.assertEqual(product_reference("4820012345678", "test_shop_bot"), ("code", "4820012345678"))
        self.assertEqual(product_reference("#1", "test_shop_bot"), ("id", "1"))
        for invalid in ["https://evil.test", "https://t.me/other_bot?start=p_1", "p_0", ""]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                product_reference(invalid, "test_shop_bot")
        with self.assertRaises(ValueError):
            decode_image(b"not an image")

    def test_box_barcode_photo_is_decoded_without_an_external_service(self):
        code = zxingcpp.create_barcode("5901234123457", zxingcpp.BarcodeFormat.EAN13)
        bitmap = zxingcpp.write_barcode_to_image(code, scale=3)
        image = Image.fromarray(bitmap)
        output = BytesIO()
        image.save(output, format="PNG")
        self.assertEqual(decode_image(output.getvalue()), ["5901234123457"])

    def test_backups_include_sales_access_and_settings_and_survive_reopen(self):
        sale = self.store.sell([dict(product_id=1, version=1, size="38", quantity=1, unit_price=100)], Actor(101, "Owner"), "s")
        self.store.grant(202)
        self.store.set_setting("report_time", "20:30")
        path = make_backup(self.store)
        copied = Store(path)
        self.assertEqual(copied.sale(sale["id"])["total"], 100)
        self.assertTrue(copied.is_staff(202))
        self.assertEqual(copied.setting("report_time"), "20:30")
        self.assertEqual(self.store.setting("last_backup_file"), str(path))

    def test_backup_interval_and_retention_never_delete_manual_files(self):
        directory = self.store.path.parent / "backups"
        directory.mkdir()
        manual = directory / "manual.sqlite3"
        manual.write_bytes(b"keep")
        now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        maintenance_tick(self.store, 0, now=now)
        maintenance_tick(self.store, 0, now=now + timedelta(minutes=10))
        self.assertEqual(len(list(directory.glob("auto-*.sqlite3"))), 1)
        for i in range(30):
            make_backup(self.store, now=now + timedelta(hours=6 * (i + 1)))
        self.assertEqual(len(list(directory.glob("auto-*.sqlite3"))), 28)
        self.assertEqual(manual.read_bytes(), b"keep")

    def test_report_uses_kyiv_time_persists_queue_and_does_not_repeat(self):
        # October 20 Kyiv is UTC+3; 18:00 UTC is 21:00 Kyiv.
        before = datetime(2026, 10, 20, 17, 59, tzinfo=timezone.utc)
        schedule_reports(self.store, 101, before)
        self.assertEqual(self.store.pending_notices(), [])
        schedule_reports(self.store, 101, before + timedelta(minutes=1))
        self.assertEqual(len(self.store.pending_notices()), 1)
        notice = self.store.pending_notices()[0]
        self.assertEqual(notice["chat_id"], 101)
        reopened = Store(self.store.path)
        schedule_reports(reopened, 101, before + timedelta(hours=2))
        self.assertEqual(len(reopened.pending_notices()), 1)
        self.assertIn("20.10.2026", notice["text"])

    def test_missed_reports_are_queued_once_after_restart_and_can_be_disabled(self):
        self.store.set_setting("last_report_day", "2026-10-17")
        now = datetime(2026, 10, 20, 18, tzinfo=timezone.utc)
        schedule_reports(self.store, 101, now)
        self.assertEqual(len(self.store.pending_notices()), 3)
        self.store.set_setting("report_time", "off")
        schedule_reports(self.store, 101, now + timedelta(days=2))
        self.assertEqual(len(self.store.pending_notices()), 3)

    def test_access_request_is_durable_rate_limited_and_stale_approval_cannot_regrant(self):
        self.assertTrue(self.store.request_access(202, "Employee", 101))
        self.assertFalse(self.store.request_access(202, "Employee", 101))
        self.assertEqual(len(self.store.pending_notices()), 1)
        request = self.store.access_requests()[0]
        self.store.decide_access(202, request["nonce"], True)
        self.assertTrue(self.store.is_staff(202))
        self.store.revoke(202)
        with self.assertRaises(NoChange):
            self.store.decide_access(202, request["nonce"], True)
        self.assertFalse(self.store.is_staff(202))

    def test_os_lock_blocks_second_process_and_releases_after_exit(self):
        lock = self.store.path.parent / "bot.lock"
        with instance_lock(lock):
            with self.assertRaises(AlreadyRunning):
                with instance_lock(lock):
                    self.fail("Second lock must not be acquired")
        with instance_lock(lock):
            pass
