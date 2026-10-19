import os
import unittest
from unittest.mock import patch

from shoe_shop_bot.config import Settings


TOKEN = "123456:private_test_token_abcdefghijklmnopqrstuvwxyz"


class ConfigTests(unittest.TestCase):
    def load(self, **values):
        with patch("shoe_shop_bot.config.load_dotenv"), patch.dict(os.environ, values, clear=True):
            return Settings.load()

    def test_token_required_and_not_shown_in_repr(self):
        with self.assertRaisesRegex(ValueError, "BOT_TOKEN"):
            self.load()
        settings = self.load(BOT_TOKEN=TOKEN, OWNER_ID="101")
        self.assertNotIn(TOKEN, repr(settings))
        with self.assertRaisesRegex(ValueError, "BOT_TOKEN"):
            self.load(BOT_TOKEN="wrong-format")

    def test_no_owner_means_no_implicit_owner(self):
        settings = self.load(BOT_TOKEN=TOKEN)
        self.assertFalse(settings.is_owner(101))
        self.assertFalse(settings.is_owner(0))

    def test_owner_group_and_staff_mode(self):
        settings = self.load(BOT_TOKEN=TOKEN, OWNER_ID="101", NOTIFICATION_MODE="staff", NOTIFICATION_CHAT_ID="-123")
        self.assertTrue(settings.is_owner(101))
        self.assertFalse(settings.is_owner(202))
        self.assertEqual(settings.notification_chat_id, -123)
        self.assertEqual(settings.notification_mode, "staff")

    def test_invalid_configuration_is_actionable(self):
        for values in [{"OWNER_ID": "username"}, {"OWNER_ID": "-1"}, {"OWNER_ID": str(2**64)}, {"NOTIFICATION_MODE": "everyone"}, {"NOTIFICATION_CHAT_ID": "123"}, {"DATABASE_PATH": ""}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load(BOT_TOKEN=TOKEN, **values)
