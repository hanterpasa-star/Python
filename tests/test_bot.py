import json
from pathlib import Path
import tempfile
import unittest

from telegram import Update
from telegram.request import BaseRequest

from shoe_shop_bot.bot import build_application
from shoe_shop_bot.config import Settings
from shoe_shop_bot.notifications import deliver_once
from shoe_shop_bot.storage import Store


FAKE_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE"


class OfflineTelegram(BaseRequest):
    """Exercise real Telegram update parsing and handlers without network/token."""
    def __init__(self):
        self.calls = []
        self.sequence = 100

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        data = request_data.parameters if request_data else {}
        self.calls.append((api_method, data))
        if api_method == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "Shop", "username": "offline_shop_bot"}
        elif api_method in {"sendMessage", "sendPhoto", "sendDocument"}:
            self.sequence += 1
            chat_id = int(data["chat_id"])
            result = {
                "message_id": self.sequence, "date": 1,
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                "from": {"id": 123456, "is_bot": True, "first_name": "Shop"},
            }
            if api_method == "sendPhoto":
                result.update(caption=data.get("caption", ""), photo=[{"file_id": "photo", "file_unique_id": "unique", "width": 100, "height": 100}])
            elif api_method == "sendDocument":
                result.update(caption=data.get("caption", ""), document={"file_id": "backup", "file_unique_id": "backup-unique", "file_name": "backup.sqlite3"})
            else:
                result["text"] = data.get("text", "")
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "shop.sqlite3")
        self.settings = Settings(FAKE_TOKEN, 101, self.store.path)
        self.request = OfflineTelegram()
        self.app = build_application(self.settings, self.store, request=self.request)
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self.errors = []

        async def capture_error(update, context):
            self.errors.append(context.error)

        self.app.add_error_handler(capture_error)
        self.sequence = 0

    async def update(self, user=101, text=None, photo=False, callback=None, chat=None, photo_id="shoe-photo-id"):
        self.sequence += 1
        chat_id = user if chat is None else chat
        person = {"id": user, "is_bot": False, "first_name": f"User{user}"}
        message = {
            "message_id": self.sequence, "date": 1,
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "from": person,
        }
        if photo:
            message["photo"] = [{"file_id": photo_id, "file_unique_id": "unique-photo", "width": 100, "height": 100}]
        if text is not None:
            message["text"] = text
            if text.startswith("/"):
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        payload = {"update_id": self.sequence}
        if callback:
            message["from"] = {"id": 123456, "is_bot": True, "first_name": "Shop"}
            payload["callback_query"] = {"id": str(self.sequence), "from": person, "chat_instance": "test", "data": callback, "message": message}
        else:
            payload["message"] = message
        await self.app.process_update(Update.de_json(payload, self.app.bot))
        self.assertEqual(self.errors, [], f"Unhandled handler errors: {self.errors}")

    def texts(self):
        return "\n".join(str(data.get("text", data.get("caption", ""))) for _, data in self.request.calls)

    async def create_product(self):
        await self.update(text="/add")
        await self.update(text="Nike Air белые")
        await self.update(photo=True)
        await self.update(text="38:2, 39, 40.5")
        nonce = self.app.user_data[101]["draft"]["nonce"]
        self.assertEqual(self.store.page(include_empty=True)[1], 0)
        await self.update(callback=f"save:{nonce}")
        return self.store.page()[0][0], nonce

    async def test_full_add_photo_sizes_confirmation_and_duplicate_click(self):
        product, nonce = await self.create_product()
        self.assertEqual(product.photo, "shoe-photo-id")
        self.assertEqual(product.stock, {"38": 2, "39": 1, "40.5": 1})
        await self.update(callback=f"save:{nonce}")
        self.assertEqual(self.store.page()[1], 1)
        await self.update(text="/stock")
        await self.update(callback=f"view:stock:{product.id}")
        self.assertIn("38 × 2", self.texts())

    async def test_unauthorized_users_cannot_see_or_edit_catalogue(self):
        product, _ = await self.create_product()
        self.request.calls.clear()
        for command in ["/start", "/stock", "/edit", "/add", "/history", "/staff", "/grant 303"]:
            await self.update(user=202, text=command)
        for callback in [f"view:stock:{product.id}", f"edit:set_sizes:{product.id}:1", f"archive:confirm:{product.id}:1", "save:anything"]:
            await self.update(user=202, callback=callback)
        self.assertNotIn("Nike", self.texts())
        self.assertFalse(any(method == "sendPhoto" for method, _ in self.request.calls))
        self.assertEqual(self.store.get(product.id).version, 1)
        self.assertFalse(self.store.is_staff(303))

    async def test_owner_grants_access_staff_cannot_grant_and_revocation_blocks_old_input(self):
        product, _ = await self.create_product()
        await self.update(text="/grant 202")
        await self.update(user=202, text="/stock")
        self.assertIn("Nike", self.texts())
        await self.update(user=202, text="/grant 303")
        self.assertFalse(self.store.is_staff(303))
        await self.update(user=202, callback=f"edit:set_sizes:{product.id}:1")
        await self.update(user=202, text="42")
        nonce = self.app.user_data[202]["draft"]["nonce"]
        await self.update(text="/revoke 202")
        self.assertNotIn("draft", self.app.user_data[202])
        await self.update(user=202, callback=f"save:{nonce}")
        self.assertEqual(self.store.get(product.id).stock, product.stock)

    async def test_two_staff_drafts_cannot_overwrite_each_other(self):
        product, _ = await self.create_product()
        await self.update(text="/grant 202")
        await self.update(callback=f"edit:add_sizes:{product.id}:1")
        await self.update(user=202, callback=f"edit:remove_sizes:{product.id}:1")
        await self.update(text="41:2")
        await self.update(user=202, text="38")
        first = self.app.user_data[101]["draft"]["nonce"]
        second = self.app.user_data[202]["draft"]["nonce"]
        await self.update(callback=f"save:{first}")
        await self.update(user=202, callback=f"save:{second}")
        result = self.store.get(product.id)
        self.assertEqual(result.stock, {"38": 2, "39": 1, "40.5": 1, "41": 2})
        self.assertEqual(result.version, 2)
        self.assertIn("Другой сотрудник", self.texts())

    async def test_group_notification_setup_is_owner_only_and_catalog_does_not_leak_to_groups(self):
        await self.update(user=202, text="/notify_here", chat=-123)
        self.assertIsNone(self.store.notification_chat())
        await self.update(text="/notify_here", chat=-123)
        self.assertEqual(self.store.notification_chat(), -123)
        product, _ = await self.create_product()
        self.request.calls.clear()
        await self.update(text="/stock", chat=-456)
        self.assertNotIn("Nike", self.texts())
        await deliver_once(self.app.bot, self.store, 101)
        notifications = [data for method, data in self.request.calls if method == "sendPhoto"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["chat_id"], -123)
        self.assertIn("38 × 2", notifications[0]["caption"])

    async def test_cancel_and_stale_preview_do_not_save(self):
        await self.update(text="/add")
        await self.update(text="Товар")
        await self.update(photo=True)
        await self.update(text="38")
        nonce = self.app.user_data[101]["draft"]["nonce"]
        await self.update(text="/cancel")
        await self.update(callback=f"save:{nonce}")
        self.assertEqual(self.store.page(include_empty=True)[1], 0)

    async def test_sizes_replaced_photo_changed_and_archive_confirmed(self):
        product, _ = await self.create_product()
        await self.update(callback=f"edit:set_sizes:{product.id}:1")
        await self.update(text="-")
        await self.update(callback=f"save:{self.app.user_data[101]['draft']['nonce']}")
        self.assertEqual(self.store.page()[1], 0)
        self.assertEqual(self.store.page(include_empty=True)[1], 1)
        await self.update(callback=f"edit:rename:{product.id}:2")
        await self.update(text="Новое название")
        await self.update(callback=f"save:{self.app.user_data[101]['draft']['nonce']}")
        self.assertEqual(self.store.get(product.id).title, "Новое название")
        await self.update(callback=f"edit:photo:{product.id}:3")
        await self.update(photo=True, photo_id="replacement-photo-id")
        await self.update(callback=f"save:{self.app.user_data[101]['draft']['nonce']}")
        self.assertEqual(self.store.get(product.id).photo, "replacement-photo-id")
        await self.update(callback=f"archive:ask:{product.id}:4")
        self.assertEqual(self.store.page(include_empty=True)[1], 1)
        await self.update(callback=f"archive:confirm:{product.id}:4")
        self.assertEqual(self.store.page(include_empty=True)[1], 0)

    async def test_id_available_before_owner_is_configured(self):
        self.app.bot_data["shop"].settings = Settings(FAKE_TOKEN, 0, self.store.path)
        await self.update(user=202, text="/id")
        self.assertIn("202", self.texts())
        await self.update(user=202, text="/start")
        self.assertIn("закрытый бот", self.texts())
        self.assertFalse(self.store.is_staff(202))

    async def test_successful_employee_removal_and_history(self):
        product, _ = await self.create_product()
        await self.update(text="/grant 202")
        await self.update(user=202, callback=f"edit:remove_sizes:{product.id}:1")
        await self.update(user=202, text="38")
        await self.update(user=202, callback=f"save:{self.app.user_data[202]['draft']['nonce']}")
        self.assertEqual(self.store.get(product.id).stock, {"39": 1, "40.5": 1})
        self.assertEqual(self.store.history()[0]["actor_id"], 202)
        await self.update(user=202, text="/history")
        self.assertIn("Убраны размеры", self.texts())

    async def test_application_startup_and_shutdown_hooks(self):
        await self.app.start()
        try:
            await self.app.post_init(self.app)
            task = self.app.bot_data["notification_task"]
            self.assertFalse(task.done())
            self.assertTrue(any(method == "setMyCommands" for method, _ in self.request.calls))
            commands = next(data["commands"] for method, data in self.request.calls if method == "setMyCommands")
            self.assertNotIn("scan", [command["command"] for command in commands])
        finally:
            await self.app.post_stop(self.app)
            await self.app.stop()
        self.assertTrue(task.cancelled())
