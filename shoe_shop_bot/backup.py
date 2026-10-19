import argparse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import os

from .config import PROJECT_ROOT
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Безопасная копия базы товаров и сотрудников.")
    parser.add_argument("--output", type=Path, default=Path("backups") / datetime.now().strftime("shop-%Y%m%d-%H%M%S.sqlite3"))
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=False, interpolate=False)
    source = Path(os.getenv("DATABASE_PATH", "data/shop.sqlite3"))
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    if not source.is_file():
        parser.error("База ещё не создана: сначала запустите бота.")
    try:
        Store(source).backup(args.output)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Копия сохранена: {args.output}")


if __name__ == "__main__":
    main()
