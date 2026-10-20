"""Docker deployment checks. Run after building shoe-shop-bot:ci."""

from pathlib import Path
import os
import subprocess
import uuid


ROOT = Path(__file__).resolve().parent.parent
IMAGE = "shoe-shop-bot:ci"


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=True, **kwargs)


def main():
    child_env = os.environ.copy()
    child_env["BOT_ENV_FILE"] = ".env.example"
    run(["docker", "compose", "config", "--quiet"], env=child_env)
    print("Compose configuration is valid; no private .env is needed for this check.", flush=True)

    missing_token = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", IMAGE],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert missing_token.returncode == 2, missing_token.stdout + missing_token.stderr
    assert "BOT_TOKEN" in missing_token.stderr
    print("Container starts the application and reports missing credentials cleanly.", flush=True)

    volume = "shoe-shop-check-" + uuid.uuid4().hex
    run(["docker", "volume", "create", volume])
    base = ["docker", "run", "--rm", "--network", "none", "--mount", f"type=volume,src={volume},dst=/var/lib/shoe-shop-bot", IMAGE, "python", "-c"]
    try:
        run(base + ["""
import os
from pathlib import Path
from shoe_shop_bot.storage import Store, Actor
assert os.getuid() == 10001
store = Store(Path(os.environ['DATABASE_PATH']))
product = store.create('Container inventory', 'offline-photo-id', {'38': 2}, Actor(101, 'Owner'), [-123])
store.grant(202)
store.set_notification_chat(-123)
store.backup(Path('/var/lib/shoe-shop-bot/backup.sqlite3'))
assert product.id == 1
"""])
        run(base + ["""
import os
from pathlib import Path
from shoe_shop_bot.storage import Store
store = Store(Path(os.environ['DATABASE_PATH']))
assert store.get(1).stock == {'38': 2}
assert store.is_staff(202)
assert store.notification_chat() == -123
assert store.delivery_counts() == {'pending': 1}
backup = Store(Path('/var/lib/shoe-shop-bot/backup.sqlite3'))
assert backup.get(1).stock == {'38': 2}
assert backup.is_staff(202)
"""])
        print("A replacement container retains inventory, staff, notifications and backup in the volume.", flush=True)
    finally:
        run(["docker", "volume", "rm", volume])


if __name__ == "__main__":
    main()
