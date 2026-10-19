import asyncio
from datetime import timedelta
import logging

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from .stock import format_stock
from .storage import Store


log = logging.getLogger(__name__)
ACTION_NAMES = {
    "create": "🆕 Добавлен товар", "add_sizes": "➕ Добавлены размеры",
    "remove_sizes": "➖ Убраны размеры", "set_sizes": "✏️ Изменено наличие",
    "rename": "✏️ Изменено название", "photo": "📷 Изменено фото",
    "archive": "🗑 Товар убран из каталога",
}


def event_text(payload: dict) -> str:
    lines = [ACTION_NAMES[payload["action"]], f'#{payload["product_id"]} · {payload["title"]}']
    if payload["action"] == "rename":
        lines.append("Прежнее название: " + payload["before_title"])
    if payload["before_stock"] is not None:
        lines.append("Было: " + format_stock(payload["before_stock"]))
    if payload["action"] == "archive":
        lines.append("Товар скрыт из списков.")
    else:
        lines.append("Сейчас: " + format_stock(payload["stock"]))
    lines.append(f'Сотрудник: {payload["actor_name"]} (ID {payload["actor_id"]})')
    return "\n".join(lines)


async def deliver_once(bot, store: Store, owner_id: int) -> None:
    for delivery in store.pending():
        event_id, chat_id = delivery["event_id"], delivery["chat_id"]
        # A revoked employee must not receive even an older queued notification.
        if chat_id > 0 and chat_id != owner_id and not store.is_staff(chat_id):
            store.delivery_result(event_id, chat_id, status="cancelled")
            continue
        try:
            text = event_text(delivery["payload"])
            if len(text) <= 1024:
                await bot.send_photo(chat_id=chat_id, photo=delivery["payload"]["photo"], caption=text)
            else:
                # Only extreme-length names/IDs need the larger message limit.
                await bot.send_message(chat_id=chat_id, text=text)
        except RetryAfter as exc:
            delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
            store.delivery_result(event_id, chat_id, status="pending", delay=delay + 1)
        except (Forbidden, BadRequest):
            store.delivery_result(event_id, chat_id, status="failed")
            log.warning("Не удалось отправить уведомление в чат %s. Владелец может проверить /status.", chat_id)
        except (NetworkError, TelegramError):
            delay = min(3600, 30 * (2 ** min(delivery["attempts"], 7)))
            store.delivery_result(event_id, chat_id, status="pending", delay=delay)
        else:
            store.delivery_result(event_id, chat_id)


async def notification_loop(bot, store: Store, owner_id: int) -> None:
    while True:
        try:
            await deliver_once(bot, store, owner_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log API URLs or token-bearing exception messages.
            log.error("Ошибка очереди уведомлений: %s", type(exc).__name__)
        await asyncio.sleep(2)
