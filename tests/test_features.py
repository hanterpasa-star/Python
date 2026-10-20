from datetime import datetime, timezone
import unittest

from shoe_shop_bot.maintenance import schedule_reports
from shoe_shop_bot.notifications import deliver_once
from shoe_shop_bot.storage import Actor
from tests import test_bot as original


class FeatureFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = original.BotFlowTests.asyncSetUp
    update = original.BotFlowTests.update
    texts = original.BotFlowTests.texts
    create_product = original.BotFlowTests.create_product

    def draft(self, user=101):
        return self.app.user_data[user]["draft"]

    async def add_sale_item(self, product, size="38", price="1500,50"):
        await self.update(callback=f"pick:{self.draft()['nonce']}:{product.id}")
        await self.update(text=size)
        await self.update(text=price)

    async def test_cart_sale_partial_return_void_and_repeat_clicks(self):
        product, _ = await self.create_product()
        await self.update(text="/sell")
        await self.add_sale_item(product, "38:2")
        nonce = self.draft()["nonce"]
        self.assertEqual(self.store.get(product.id).stock["38"], 2)
        await self.update(callback=f"sellpage:{nonce}:0")
        await self.add_sale_item(product, "39", "1000")
        await self.update(callback=f"checkout:{nonce}")
        sale = self.store.sale(1)
        self.assertEqual(sale["total"], 400100)
        self.assertEqual(self.store.get(product.id).stock, {"40.5": 1})
        await self.update(callback=f"checkout:{nonce}")
        self.assertEqual(self.store.sales_page()[1], 1)
        item_id = sale["items"][0]["id"]
        await self.update(callback=f"return:1:{item_id}")
        await self.update(text="1")
        nonce = self.draft()["nonce"]
        await self.update(callback=f"returnok:{nonce}")
        await self.update(callback=f"returnok:{nonce}")
        self.assertEqual(self.store.get(product.id).stock["38"], 1)
        await self.update(callback="voidask:1")
        nonce = self.draft()["nonce"]
        await self.update(callback=f"voidok:{nonce}")
        await self.update(callback=f"voidok:{nonce}")
        self.assertEqual(self.store.get(product.id).stock, product.stock)
        self.assertEqual(self.store.report("2000", "2100")["net_amount"], 0)

    async def test_invalid_price_cancel_and_empty_cart_do_not_touch_stock(self):
        product, _ = await self.create_product()
        await self.update(text="/cart")
        self.assertIn("Текущей корзины нет", self.texts())
        await self.update(text="/sell")
        await self.update(callback=f"pick:{self.draft()['nonce']}:{product.id}")
        await self.update(text="38:3")
        self.assertEqual(self.draft()["stage"], "quantity")
        await self.update(text="38")
        await self.update(text="-15")
        self.assertEqual(self.draft()["stage"], "price")
        await self.update(text="/cancel")
        self.assertEqual(self.store.get(product.id), product)

    async def test_stale_cart_is_rejected_after_another_staff_edit(self):
        product, _ = await self.create_product()
        await self.update(text="/sell")
        await self.add_sale_item(product)
        nonce = self.draft()["nonce"]
        self.store.change(product.id, 1, "add_sizes", {"41": 1}, Actor(202, "Employee"))
        await self.update(callback=f"checkout:{nonce}")
        self.assertEqual(self.store.sales_page()[1], 0)
        self.assertIn("Другой сотрудник", self.texts())
        self.assertEqual(self.store.get(product.id).stock["38"], 2)

    async def test_access_request_owner_approval_and_stale_button_after_revoke(self):
        await self.update(user=202, text="/start")
        await self.update(user=202, callback="request_access")
        await self.update(user=202, callback="request_access")
        self.assertEqual(len(self.store.access_requests()), 1)
        request = self.store.access_requests()[0]
        callback = f"access:yes:202:{request['nonce']}"
        await deliver_once(self.app.bot, self.store, 101)
        self.assertIn("Запрос доступа\nUser202", self.texts())
        await self.update(user=303, callback=callback)
        self.assertFalse(self.store.is_staff(202))
        await self.update(callback=callback)
        self.assertTrue(self.store.is_staff(202))
        await self.update(text="/revoke 202")
        await self.update(callback=callback)
        self.assertFalse(self.store.is_staff(202))

    async def test_denied_request_does_not_grant_access(self):
        await self.update(user=202, text="/request_access")
        nonce = self.store.access_requests()[0]["nonce"]
        await self.update(callback=f"access:no:202:{nonce}")
        self.assertFalse(self.store.is_staff(202))
        self.assertEqual(self.store.access_requests(), [])
        await self.update(user=202, text="/request_access")
        self.assertIn("через 24 часа", self.texts())

    async def test_all_new_operations_check_access_and_financial_data_does_not_leak(self):
        product, _ = await self.create_product()
        await self.update(text="/sell")
        await self.add_sale_item(product)
        nonce = self.draft()["nonce"]
        self.request.calls.clear()
        for command in ["/sell", "/cart", "/sales", "/find Nike", "/sections", "/scan p_1", "/summary", "/report", "/report_time 19:00", "/backup", "/requests", "/status"]:
            await self.update(user=202, text=command)
        for callback in ["sale:1", "return:1:1", "voidask:1", "report:month", "qr:1", f"checkout:{nonce}"]:
            await self.update(user=202, callback=callback)
        self.assertNotIn("Nike", self.texts())
        self.assertFalse(any(method in {"sendPhoto", "sendDocument"} for method, _ in self.request.calls))
        self.assertEqual(self.store.sales_page()[1], 0)

    async def test_optional_codes_create_without_them_then_search_sections_and_qr(self):
        product, _ = await self.create_product()
        self.assertEqual(product.code, "")
        await self.update(callback=f"edit:category:{product.id}:1")
        await self.update(text="Nike")
        await self.update(callback=f"save:{self.draft()['nonce']}")
        await self.update(callback=f"edit:code:{product.id}:2")
        await self.update(text="4820012345678")
        await self.update(callback=f"save:{self.draft()['nonce']}")
        await self.update(text="/find БЕЛЫЕ")
        self.assertIn("Фильтр: БЕЛЫЕ", self.texts())
        await self.update(text="/sections")
        nonce = self.app.user_data[101]["sections"]["nonce"]
        await self.update(callback=f"section:{nonce}:0")
        self.assertIn("Фильтр: Nike", self.texts())
        await self.update(text="/scan 4820012345678")
        await self.update(text="/start p_1")
        await self.update(callback="qr:1")
        self.assertIn("QR товара #1", self.texts())
        self.request.calls.clear()
        await self.update(user=202, text="/start p_1")
        self.assertFalse(any(method == "sendPhoto" for method, _ in self.request.calls))

    async def test_duplicate_warning_is_visible_before_save_and_can_be_cancelled(self):
        product, _ = await self.create_product()
        await self.update(text="/add")
        await self.update(text=product.title.upper())
        await self.update(photo=True)
        await self.update(text="38")
        self.assertIn("Уже есть модель с таким названием: #1", self.texts())
        await self.update(text="/cancel")
        self.assertEqual(self.store.page(include_empty=True)[1], 1)

    async def test_reports_custom_dates_summary_owner_backup_and_time_settings(self):
        product, _ = await self.create_product()
        await self.update(text="/sell")
        await self.add_sale_item(product)
        await self.update(callback=f"checkout:{self.draft()['nonce']}")
        await self.update(callback="report:today")
        self.assertIn("Продано пар: 1", self.texts())
        await self.update(callback="report:custom")
        await self.update(text="01.01.2020 31.12.2030")
        await self.update(text="/report 01.01.2026 31.12.2026")
        self.assertIn("1 500,50 грн", self.texts())
        await self.update(text="/summary")
        self.assertIn("Всего пар: 3", self.texts())
        await self.update(text="/report_time 25:00")
        self.assertIsNone(self.store.setting("report_time"))
        await self.update(text="/report_time 20:30")
        self.assertEqual(self.store.setting("report_time"), "20:30")
        await self.update(text="/backup")
        self.assertTrue(any(method == "sendDocument" for method, _ in self.request.calls))
        await self.update(text="/status")
        self.assertIn("Последняя резервная копия", self.texts())
        await self.update(text="/grant 202")
        self.request.calls.clear()
        await self.update(user=202, text="/backup")
        await self.update(user=202, text="/report_time off")
        self.assertFalse(any(method == "sendDocument" for method, _ in self.request.calls))
        self.assertEqual(self.store.setting("report_time"), "20:30")

    async def test_scheduled_reports_are_delivered_only_to_owner(self):
        self.store.set_setting("last_report_day", "2026-10-19")
        schedule_reports(self.store, 101, datetime(2026, 10, 20, 18, tzinfo=timezone.utc))
        await deliver_once(self.app.bot, self.store, 101)
        await deliver_once(self.app.bot, self.store, 101)
        reports = [data for method, data in self.request.calls if method == "sendMessage" and "Вечерний отчёт" in data.get("text", "")]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["chat_id"], 101)

    async def test_alphabetical_default_and_switchable_sorting(self):
        self.store.create("Яркие", "photo", {"36": 1}, Actor(101, "Owner"))
        self.store.create("Белые", "photo", {"40": 1}, Actor(101, "Owner"))
        await self.update(text="/stock")
        buttons = self.request.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertIn("Белые", buttons[0][0]["text"])
        await self.update(callback="sort:size:stock")
        buttons = self.request.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertIn("Яркие", buttons[0][0]["text"])
        await self.update(callback="sort:newest:stock")
        self.assertEqual(self.app.user_data[101]["catalog_sort"], "newest")
