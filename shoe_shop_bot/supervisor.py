"""Optional local supervisor used by the Windows logon task."""
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import time

from .config import PROJECT_ROOT, Settings
from .process import AlreadyRunning, instance_lock


def main():
    try:
        settings = Settings.load()
    except ValueError:
        return 2
    directory = settings.database_path.parent / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(directory / "supervisor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")], format="%(asctime)s %(message)s")
    try:
        with instance_lock(settings.database_path.parent / "supervisor.lock"):
            child_env = dict(os.environ, BOT_BACKGROUND="1", PYTHONUTF8="1")
            while True:
                process = subprocess.Popen([sys.executable, "-m", "shoe_shop_bot"], cwd=PROJECT_ROOT,
                                           env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    code = process.wait()
                except (KeyboardInterrupt, SystemExit):
                    process.terminate()
                    process.wait(timeout=30)
                    return 0
                # Configuration errors, intentional shutdown, or another instance need no retries.
                if code in (0, 2, 3):
                    logging.info("Bot stopped with code %s; no restart needed.", code)
                    return 0
                logging.warning("Bot exited with code %s; restarting in 10 seconds.", code)
                time.sleep(10)
    except AlreadyRunning:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
