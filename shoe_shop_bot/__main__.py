import logging
import sys

from telegram.error import InvalidToken, NetworkError, TelegramError

from .bot import build_application
from .config import Settings


class TokenSafeFormatter(logging.Formatter):
    def __init__(self, token: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record):
        return super().format(record).replace(self.token, "[TOKEN HIDDEN]")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP request URLs contain the bot token; never print them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Expected startup/API failures are reported below and by our error handler.
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    try:
        settings = Settings.load()
    except ValueError as exc:
        print(f"Настройка: {exc}", file=sys.stderr)
        return 2
    for handler in logging.getLogger().handlers:
        handler.setFormatter(TokenSafeFormatter(settings.token))
    try:
        application = build_application(settings)
        application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
    except InvalidToken:
        print("Telegram не принял токен. Проверьте BOT_TOKEN в .env. Сам токен не отправляйте в чат.", file=sys.stderr)
        return 2
    except NetworkError:
        print("Не удалось подключиться к Telegram. Проверьте интернет и повторите запуск.", file=sys.stderr)
        return 1
    except TelegramError:
        print("Telegram отклонил запуск бота. Проверьте токен, сеть и отсутствие другого запущенного экземпляра.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
