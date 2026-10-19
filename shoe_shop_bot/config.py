from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    owner_id: int
    database_path: Path
    notification_mode: str = "group"
    notification_chat_id: int | None = None

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id > 0 and user_id == self.owner_id

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env", override=False, interpolate=False)
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError(
                "Не заполнен BOT_TOKEN. Скопируйте .env.example в .env "
                "и укажите токен из @BotFather. Не отправляйте токен в чат."
            )
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("BOT_TOKEN не похож на токен из @BotFather. Вставьте его целиком, без кавычек и пробелов.")
        try:
            owner_id = int(os.getenv("OWNER_ID", "").strip() or "0")
            chat_id = os.getenv("NOTIFICATION_CHAT_ID", "").strip()
            chat_id = int(chat_id) if chat_id else None
        except ValueError:
            raise ValueError("OWNER_ID и NOTIFICATION_CHAT_ID должны содержать числовые ID.") from None
        if not 0 <= owner_id < 2**63:
            raise ValueError("В OWNER_ID нужен положительный ID владельца Telegram.")
        if chat_id is not None and not -(2**63) < chat_id < 0:
            raise ValueError("NOTIFICATION_CHAT_ID должен быть отрицательным ID группы. Можно оставить поле пустым и вызвать /notify_here в группе.")
        notification_mode = os.getenv("NOTIFICATION_MODE", "group").strip()
        if notification_mode not in {"group", "staff"}:
            raise ValueError("NOTIFICATION_MODE должен быть group или staff.")
        path_text = os.getenv("DATABASE_PATH", "data/shop.sqlite3").strip()
        if not path_text:
            raise ValueError("Укажите DATABASE_PATH, например data/shop.sqlite3.")
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return cls(token, owner_id, path, notification_mode, chat_id)
