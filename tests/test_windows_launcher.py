import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class WindowsLauncherTests(unittest.TestCase):
    def test_cmd_files_have_portable_encoding_and_line_endings(self):
        for name in ["START_BOT.bat", "EDIT_SETTINGS.bat"]:
            with self.subTest(name=name):
                content = (ROOT / name).read_bytes()
                content.decode("ascii")
                self.assertGreater(content.count(b"\r\n"), 0)
                self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
                self.assertNotIn(b"\r", content.replace(b"\r\n", b""))

    @unittest.skipUnless(os.name == "nt", "Requires real Windows CMD")
    def test_windows_launcher_preserves_settings_and_reaches_configuration_check(self):
        with tempfile.TemporaryDirectory(prefix="shop windows ") as temporary:
            checkout = Path(temporary) / "shop"
            shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns(".git", ".venv", ".env", "__pycache__", "data", "backups"))
            # Reuse the tested installed packages without relying on network access.
            subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(checkout / ".venv")], check=True, timeout=60)
            settings = b"# Keep this file unchanged\r\nBOT_TOKEN=\r\nOWNER_ID=\r\n"
            (checkout / ".env").write_bytes(settings)
            child_env = os.environ.copy()
            for name in ["BOT_TOKEN", "OWNER_ID", "DATABASE_PATH", "NOTIFICATION_MODE", "NOTIFICATION_CHAT_ID"]:
                child_env.pop(name, None)
            child_env["PIP_NO_INDEX"] = "1"
            child_env["PYTHONUTF8"] = "1"
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "START_BOT.bat", "--no-pause"],
                cwd=checkout, env=child_env, capture_output=True, timeout=60,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1, output.decode("utf-8", errors="replace"))
            self.assertIn(b"BOT_TOKEN", output)
            self.assertNotIn(b"is not recognized", output)
            self.assertEqual((checkout / ".env").read_bytes(), settings)
            self.assertFalse((checkout / "data").exists())
