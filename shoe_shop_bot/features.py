"""Employee workflows layered onto the original stock editor."""
import asyncio
from datetime import datetime, timedelta
import re
import secrets
import socket
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.error import TelegramError

from .codes import decode_image, product_reference, qr_image
from .commerce import KYIV, date_bounds, money, parse_date, parse_money, report_text
from .stock import parse_stock


SELL_BUTTON = "💰 Продано"
SEARCH_BUTTON = "🔎 Поиск"
REPORT_BUTTON = "📊 Отчёты"
SECTIONS_BUTTON = "🗂 Разделы"
SUMMARY_BUTTON = "📋 Остатки"
SALES_BUTTON = "🧾 Продажи и возвраты"
SCAN_BUTTON = "🏷 Сканировать код"


def new_draft(kind, stage, **fields):
    return dict(kind=kind, stage=stage, nonce=secrets.token_hex(8), **fields)


async def reply_chunks(message, text, **kwargs):
    # Keep lines intact and reserve room below Telegram's 4096 character limit.
    chunk = ""
    for line in text.splitlines():
        if len(chunk) + len(line) > 3800:
            await message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    await message.reply_text(chunk.rstrip() or "—", **kwargs)


class BotFeatures:
    async def request_access(self, update, context):
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text("Отправьте запрос в личной переписке с ботом.")
            return
        if self.allowed(update.effective_user.id):
            return await self.start(update, context)
        try:
            sent = self.store.request_access(update.effective_user.id, update.effective_user.full_name, self.settings.owner_id)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await update.effective_message.reply_text("Запрос отправлен владельцу. После одобрения откройте /start." if sent else "Запрос уже отправлен. Повторный запрос после отказа доступен через 24 часа.")

    async def requests(self, update, context):
        if not await self.require_access(update, context, owner=True):
            return
        requests = self.store.access_requests()
        if not requests:
            await update.effective_message.reply_text("Новых запросов доступа нет.")
        for item in requests:
            await update.effective_message.reply_text(
                f"{item['name']} · ID {item['user_id']}", reply_markup=Keyboard([[
                    Button("✅ Разрешить", callback_data=f"access:yes:{item['user_id']}:{item['nonce']}"),
                    Button("❌ Отказать", callback_data=f"access:no:{item['user_id']}:{item['nonce']}"),
                ]]))

    async def access_callback(self, update, context):
        data = update.callback_query.data or ""
        if data == "request_access":
            await update.callback_query.answer()
            await self.request_access(update, context)
            return True
        if not data.startswith("access:"):
            return False
        if not await self.require_access(update, context, owner=True):
            return True
        await update.callback_query.answer()
        from .storage import NoChange
        try:
            _, decision, user_id, nonce = data.split(":")
            if decision not in {"yes", "no"}:
                raise ValueError
            self.store.decide_access(int(user_id), nonce, decision == "yes")
        except (ValueError, NoChange, OverflowError):
            await update.effective_message.reply_text("Запрос уже обработан или устарел. Актуальные запросы: /requests")
            return True
        approved = decision == "yes"
        await update.effective_message.reply_text(f"Доступ для {user_id} " + ("разрешён ✅" if approved else "не выдан."))
        try:
            await context.bot.send_message(chat_id=int(user_id), text="Владелец разрешил доступ ✅ Нажмите /start." if approved else "Владелец отклонил запрос доступа.")
        except TelegramError:
            pass  # Decision is persisted; /start always reflects it.
        return True

    async def find(self, update, context):
        if not await self.require_access(update, context):
            return
        query = " ".join(context.args or [])
        if query:
            context.user_data["catalog_filter"] = {"query": query[:100]}
            return await self.listing(update, context, "edit")
        context.user_data["draft"] = new_draft("search", "input")
        await update.effective_message.reply_text("Введите название, размер (например 38), номер товара (#12) или штрихкод.\n/cancel — отмена.")

    async def sections(self, update, context, page=0):
        if not await self.require_access(update, context):
            return
        context.user_data.pop("draft", None)
        categories = self.store.categories()
        nonce = secrets.token_hex(4)
        context.user_data["sections"] = dict(nonce=nonce, categories=categories)
        page = max(0, min(page, max(0, (len(categories) - 1) // 8)))
        rows = [[Button(f"{item['category'] or 'Без раздела'} ({item['models']})", callback_data=f"section:{nonce}:{i}")]
                for i, item in enumerate(categories) if page * 8 <= i < (page + 1) * 8]
        nav = []
        if page:
            nav.append(Button("←", callback_data=f"sections:{page - 1}"))
        if (page + 1) * 8 < len(categories):
            nav.append(Button("→", callback_data=f"sections:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([Button("Все товары", callback_data="all_products")])
        await update.effective_message.reply_text("🗂 Выберите раздел.\nНазначить раздел товару: /edit → товар → «Раздел»." if categories else "Разделов пока нет. Добавьте товар через /add.", reply_markup=Keyboard(rows))

    async def summary(self, update, context):
        if not await self.require_access(update, context):
            return
        result = self.store.summary()
        text = f"📋 Остатки сейчас\nВсего моделей: {result['models']}\nМоделей в наличии: {result['available']}\nВсего пар: {result['pairs']}\n\n"
        text += "\n".join(f"{name}: {pairs} пар" for name, pairs in result["categories"].most_common())
        await reply_chunks(update.effective_message, text)

    async def scan(self, update, context):
        if not await self.require_access(update, context):
            return
        context.user_data["draft"] = new_draft("scan", "input")
        if context.args:
            return await self.scan_input(update, context, " ".join(context.args))
        await update.effective_message.reply_text("Пришлите фото одного штрихкода или QR-кода. Можно просто написать код цифрами.\nПривязать штрихкод: /edit → товар → «Код».\n/cancel — отмена.")

    async def scan_input(self, update, context, text):
        from .storage import NotFound
        message = update.effective_message
        if message.photo:
            photo = message.photo[-1]
            if photo.file_size and photo.file_size > 10 * 1024 * 1024:
                raise ValueError("Пришлите фото до 10 МБ.")
            file = await photo.get_file()
            data = await file.download_as_bytearray()
            text = (await asyncio.to_thread(decode_image, bytes(data)))[0]
        kind, value = product_reference(text, context.bot.username)
        try:
            product = self.store.get(int(value)) if kind == "id" else self.store.by_code(value)
        except NotFound:
            raise ValueError("Товар с таким кодом не найден. Проверьте код или привяжите его к товару через /edit.") from None
        context.user_data.pop("draft", None)
        await self.show_product(update, context, product.id, "edit")

    async def sell(self, update, context):
        if not await self.require_access(update, context):
            return
        context.user_data["draft"] = new_draft("sale", "pick", items=[])
        await self.sale_picker(update, context)

    def sale_draft(self, context, nonce=None):
        draft = context.user_data.get("draft", {})
        if draft.get("kind") != "sale" or nonce is not None and draft.get("nonce") != nonce:
            raise ValueError("Эта корзина устарела. Создайте новую через /sell.")
        return draft

    async def sale_picker(self, update, context, page=0):
        draft = self.sale_draft(context)
        draft["stage"] = "pick"
        products, count = self.store.page(offset=page * 6)
        rows = [[Button(f"#{p.id} {p.title[:35]} · {','.join(p.stock)[:30]}", callback_data=f"pick:{draft['nonce']}:{p.id}")] for p in products]
        nav = []
        if page > 0:
            nav.append(Button("←", callback_data=f"sellpage:{draft['nonce']}:{page - 1}"))
        if (page + 1) * 6 < count:
            nav.append(Button("→", callback_data=f"sellpage:{draft['nonce']}:{page + 1}"))
        if nav:
            rows.append(nav)
        if draft["items"]:
            rows.append([Button("🛒 К корзине", callback_data=f"cart:{draft['nonce']}")])
        rows.append([Button("Отмена", callback_data="cancel")])
        await update.effective_message.reply_text("Выберите товар для продажи. Можно написать часть названия для поиска." if products else "В наличии ничего не найдено. /cancel — отмена.", reply_markup=Keyboard(rows))

    async def pick_product(self, update, context, product_id, nonce):
        draft = self.sale_draft(context, nonce)
        if len(draft["items"]) >= 20:
            raise ValueError("В одной продаже максимум 20 позиций. Сохраните эту продажу и создайте следующую.")
        product = self.store.get(product_id)
        if not product.stock:
            raise ValueError("Этот товар уже закончился. Выберите другой.")
        draft.update(stage="quantity", product_id=product.id, version=product.version, title=product.title)
        await update.effective_message.reply_photo(product.photo, caption=f"{self.card(product)}\n\nКакой размер и сколько пар продано?\nНапример: 38:2 — две пары размера 38.\nОдну пару можно указать просто: 38.")

    @staticmethod
    def cart_text(draft):
        lines = ["🛒 Проверьте продажу:"]
        for i, item in enumerate(draft["items"], 1):
            lines.append(f"{i}. #{item['product_id']} {item['title']} · {item['size']} × {item['quantity']} по {money(item['unit_price'])}")
        lines.append("\nИтого: " + money(sum(i["quantity"] * i["unit_price"] for i in draft["items"])))
        return "\n".join(lines)

    async def cart(self, update, context):
        if not await self.require_access(update, context):
            return
        try:
            draft = self.sale_draft(context)
        except ValueError:
            await update.effective_message.reply_text("Текущей корзины нет. /sell — новая продажа.")
            return
        if not draft["items"]:
            return await self.sale_picker(update, context)
        draft["stage"] = "cart"
        nonce = draft["nonce"]
        await reply_chunks(update.effective_message, self.cart_text(draft), reply_markup=Keyboard([
            [Button("➕ Ещё товар / размер", callback_data=f"sellpage:{nonce}:0")],
            [Button("✅ Подтвердить продажу", callback_data=f"checkout:{nonce}")],
            [Button("Убрать последнюю позицию", callback_data=f"cartremove:{nonce}")],
            [Button("Отмена", callback_data="cancel")],
        ]))

    async def sale_input(self, update, context, draft, text):
        if draft["stage"] == "pick":
            products, total = self.store.page(query=text[:100])
            rows = [[Button(f"#{p.id} {p.title[:45]}", callback_data=f"pick:{draft['nonce']}:{p.id}")] for p in products]
            rows.append([Button("Все товары", callback_data=f"sellpage:{draft['nonce']}:0")])
            await update.effective_message.reply_text(f"Найдено: {total}." + (" Уточните название, если нужного товара нет среди первых шести." if total > 6 else ""), reply_markup=Keyboard(rows))
        elif draft["stage"] == "quantity":
            stock = parse_stock(text)
            if len(stock) != 1:
                raise ValueError("Укажите один размер и количество: 38:2. Другой размер добавите следующей позицией.")
            size, quantity = next(iter(stock.items()))
            product = self.store.get(draft["product_id"])
            already = sum(i["quantity"] for i in draft["items"] if i["product_id"] == product.id and i["size"] == size)
            if quantity + already > product.stock.get(size, 0):
                raise ValueError("Столько пар нет в наличии с учётом корзины. Проверьте размер и количество.")
            draft.update(size=size, quantity=quantity, stage="price")
            await update.effective_message.reply_text(f"Цена за ОДНУ пару в гривнах?\nНапример: 1500 или 1500,50.\nРазмер {size}, пар: {quantity}.")
        elif draft["stage"] == "price":
            price = parse_money(text)
            item = {key: draft[key] for key in ("product_id", "version", "title", "size", "quantity")}
            item["unit_price"] = price
            draft["items"].append(item)
            await self.cart(update, context)
        else:
            await update.effective_message.reply_text("Проверьте корзину и нажмите «Подтвердить продажу», добавьте ещё товар или /cancel.")

    async def checkout(self, update, context, nonce):
        draft = self.sale_draft(context, nonce)
        if draft["stage"] != "cart":
            raise ValueError("Сначала завершите ввод позиции и откройте /cart.")
        sale = self.store.sell(draft["items"], self.actor(update), nonce, self.targets())
        context.user_data.pop("draft", None)
        await update.effective_message.reply_text(f"Продажа №{sale['id']} сохранена ✅\n{money(sale['total'])}\nОстатки уменьшены.", reply_markup=self.menu(update.effective_user.id))
        await self.show_sale(update, context, sale["id"])

    @staticmethod
    def sale_text(sale):
        stamp = datetime.fromisoformat(sale["created_at"]).astimezone(KYIV).strftime("%d.%m.%Y %H:%M")
        lines = [f"🧾 Продажа №{sale['id']} · {stamp} (Киев)", f"Сотрудник: {sale['actor_name']}"]
        if sale["status"] == "void":
            lines.append("⏪ Продажа отменена. Остатки восстановлены.")
        for item in sale["items"]:
            lines.append(f"#{item['product_id']} {item['title']} · {item['size']} × {item['quantity']} по {money(item['unit_price'])}" + (f" · возврат: {item['returned']}" if item["returned"] else ""))
        lines.append("Исходная сумма: " + money(sale["total"]))
        retained = 0 if sale["status"] == "void" else sum((i["quantity"] - i["returned"]) * i["unit_price"] for i in sale["items"])
        lines.append("После возвратов / отмены: " + money(retained))
        return "\n".join(lines)

    async def sales(self, update, context, page=0):
        if not await self.require_access(update, context):
            return
        context.user_data.pop("draft", None)
        sales, count = self.store.sales_page(offset=page * 6)
        rows = [[Button(f"№{s['id']} · {s['created_at'][:10]} · {money(s['total'])}" + (" · отменена" if s["status"] == "void" else ""), callback_data=f"sale:{s['id']}")] for s in sales]
        nav = []
        if page:
            nav.append(Button("←", callback_data=f"sales:{page - 1}"))
        if (page + 1) * 6 < count:
            nav.append(Button("→", callback_data=f"sales:{page + 1}"))
        if nav:
            rows.append(nav)
        await update.effective_message.reply_text("🧾 Выберите продажу для просмотра, возврата или отмены." if sales else "Продаж пока нет. /sell — новая продажа.", reply_markup=Keyboard(rows) if rows else None)

    async def show_sale(self, update, context, sale_id):
        sale = self.store.sale(sale_id)
        rows = []
        if sale["status"] == "sold":
            for item in sale["items"]:
                if item["quantity"] > item["returned"]:
                    rows.append([Button(f"↩️ Вернуть {item['title'][:25]} · {item['size']}", callback_data=f"return:{sale_id}:{item['id']}")])
            rows.append([Button("⏪ Отменить ошибочную продажу", callback_data=f"voidask:{sale_id}")])
        rows.append([Button("← К продажам", callback_data="sales:0")])
        await reply_chunks(update.effective_message, self.sale_text(sale), reply_markup=Keyboard(rows))

    async def begin_return(self, update, context, sale_id, item_id):
        sale = self.store.sale(sale_id)
        item = next((i for i in sale["items"] if i["id"] == item_id), None)
        if sale["status"] != "sold" or item is None or item["quantity"] <= item["returned"]:
            raise ValueError("Эту позицию уже вернули или продажа отменена. Откройте /sales.")
        context.user_data["draft"] = new_draft("return", "quantity", sale_id=sale_id, item_id=item_id)
        await update.effective_message.reply_text(f"Возврат: {item['title']}, размер {item['size']}.\nМожно вернуть пар: {item['quantity'] - item['returned']}.\nСколько пар вернули? /cancel — отмена.")

    async def return_input(self, update, context, draft, text):
        if draft["stage"] != "quantity":
            await update.effective_message.reply_text("Нажмите «Подтвердить возврат» или /cancel.")
            return
        if not re.fullmatch(r"[0-9]{1,4}", text.strip()) or int(text) <= 0:
            raise ValueError("Количество пар — целое число больше 0.")
        quantity = int(text)
        sale = self.store.sale(draft["sale_id"])
        item = next(i for i in sale["items"] if i["id"] == draft["item_id"])
        if sale["status"] != "sold" or quantity > item["quantity"] - item["returned"]:
            raise ValueError("Такое количество вернуть нельзя. Проверьте /sales.")
        draft.update(quantity=quantity, stage="ready")
        await update.effective_message.reply_text(f"Вернуть {quantity} пар, размер {item['size']}?\nВозврат денег: {money(quantity * item['unit_price'])}.\nОстатки увеличатся после подтверждения.", reply_markup=Keyboard([
            [Button("✅ Подтвердить возврат", callback_data=f"returnok:{draft['nonce']}")], [Button("Отмена", callback_data="cancel")]]))

    async def reports(self, update, context):
        if not await self.require_access(update, context):
            return
        if context.args:
            try:
                if len(context.args) != 2:
                    raise ValueError("Укажите две даты: /report 01.10.2026 20.10.2026")
                return await self.send_report(update, parse_date(context.args[0]), parse_date(context.args[1]))
            except ValueError as exc:
                await update.effective_message.reply_text(str(exc))
                return
        await update.effective_message.reply_text("📊 За какой период показать продажи, выручку и популярные модели / размеры?", reply_markup=Keyboard([
            [Button("Сегодня", callback_data="report:today"), Button("Вчера", callback_data="report:yesterday")],
            [Button("Эта неделя", callback_data="report:week"), Button("Этот месяц", callback_data="report:month")],
            [Button("Выбрать даты", callback_data="report:custom")],
        ]))

    async def send_report(self, update, first, last):
        report = self.store.report(*date_bounds(first, last))
        await reply_chunks(update.effective_message, report_text(report, first, last))

    async def report_time(self, update, context):
        if not await self.require_access(update, context, owner=True):
            return
        value = " ".join(context.args or [])
        if not value:
            await update.effective_message.reply_text(f"Вечерний отчёт: {self.store.setting('report_time', '21:00')} по Киеву.\nПриходит владельцу в личные сообщения.\nИзменить: /report_time 21:30\nОтключить: /report_time off\nОтчёт включает операции до времени отправки; полный день смотрите завтра через /report.")
            return
        if value != "off" and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            await update.effective_message.reply_text("Укажите время: /report_time 21:30 (по Киеву). Отключить: /report_time off")
            return
        self.store.set_setting("report_time", value)
        await update.effective_message.reply_text("Вечерний отчёт отключён." if value == "off" else f"Отчёт будет приходить владельцу в {value} по Киеву. Уже отправленный сегодня отчёт повторно не придёт.")

    async def backup_command(self, update, context):
        if not await self.require_access(update, context, owner=True):
            return
        from .maintenance import make_backup
        path = await asyncio.to_thread(make_backup, self.store)
        with path.open("rb") as document:
            await update.effective_message.reply_document(document, filename=path.name,
                caption="Резервная копия ✅ Сохраните файл отдельно от ноутбука. В нём каталог, сотрудники, продажи и настройки; токен в файл не входит.")

    async def status_features(self, update, context):
        if not await self.require_access(update, context, owner=True):
            return
        elapsed = int(time.monotonic() - self.started_at)
        hours, rem = divmod(elapsed, 3600)
        counts = self.store.delivery_counts()
        notices = self.store.pending_notices()
        backup = self.store.setting("last_backup_time")
        stamp = datetime.fromisoformat(backup).astimezone(KYIV).strftime("%d.%m.%Y %H:%M (Киев)") if backup else "ещё нет"
        await update.effective_message.reply_text(
            f"Бот работает ✅\nУстройство: {socket.gethostname()}\nБез перезапуска: {hours} ч {rem // 60} мин\nСотрудников: {len(self.store.staff_ids())}\n"
            f"Последняя резервная копия: {stamp}\nКопия создаётся при запуске и каждые 6 часов.\n"
            f"Вечерний отчёт: {self.store.setting('report_time', '21:00')} (Киев), владельцу\n"
            f"Уведомления: отправлено {counts.get('sent', 0)}, ожидает {counts.get('pending', 0)}, ошибок {counts.get('failed', 0)}\n"
            f"Служебных сообщений в очереди: {len(notices)}\nПолучателей изменений: {len(self.targets())}\n"
            "Если бот заблокирован получателем, разблокируйте его. /backup — получить свежую копию.")

    async def feature_message(self, update, context, draft, text):
        if draft["kind"] == "search":
            if not text.strip():
                raise ValueError("Введите название, размер или номер товара.")
            context.user_data["catalog_filter"] = {"query": text[:100]}
            await self.listing(update, context, "edit")
        elif draft["kind"] == "scan":
            await self.scan_input(update, context, text)
        elif draft["kind"] == "sale":
            await self.sale_input(update, context, draft, text)
        elif draft["kind"] == "return":
            await self.return_input(update, context, draft, text)
        elif draft["kind"] == "report":
            dates = text.split()
            if len(dates) != 2:
                raise ValueError("Введите две даты через пробел: 01.10.2026 20.10.2026")
            await self.send_report(update, parse_date(dates[0]), parse_date(dates[1]))
            context.user_data.pop("draft", None)
        elif draft["kind"] == "void":
            await update.effective_message.reply_text("Нажмите кнопку подтверждения отмены продажи или /cancel.")
        else:
            return False
        return True

    async def feature_callback(self, update, context, parts):
        name = parts[0]
        if name == "sortmenu" and len(parts) == 2 and parts[1] in {"stock", "edit"}:
            await update.effective_message.reply_text("Как расположить товары? Разделы по брендам: /sections.", reply_markup=Keyboard([
                [Button(label, callback_data=f"sort:{key}:{parts[1]}")]
                for key, label in [("title", "По названию (А–Я)"), ("newest", "Сначала новые"), ("size", "По наименьшему доступному размеру")]
            ]))
        elif name == "sort" and len(parts) == 3 and parts[1] in {"title", "newest", "size"} and parts[2] in {"stock", "edit"}:
            context.user_data["catalog_sort"] = parts[1]
            await self.listing(update, context, parts[2])
        elif name == "pick" and len(parts) == 3:
            await self.pick_product(update, context, int(parts[2]), parts[1])
        elif name == "sellp" and len(parts) == 2:
            context.user_data["draft"] = new_draft("sale", "pick", items=[])
            await self.pick_product(update, context, int(parts[1]), context.user_data["draft"]["nonce"])
        elif name == "sellpage" and len(parts) == 3:
            self.sale_draft(context, parts[1])
            page = int(parts[2])
            if not 0 <= page <= 100000:
                raise ValueError("Некорректная страница.")
            await self.sale_picker(update, context, page)
        elif name in {"cart", "cartremove", "checkout"} and len(parts) == 2:
            draft = self.sale_draft(context, parts[1])
            if name == "checkout":
                await self.checkout(update, context, parts[1])
            else:
                if name == "cartremove" and draft["items"]:
                    draft["items"].pop()
                await self.cart(update, context)
        elif name == "sales" and len(parts) == 2:
            page = int(parts[1])
            if not 0 <= page <= 100000:
                raise ValueError("Некорректная страница.")
            await self.sales(update, context, page)
        elif name == "sale" and len(parts) == 2:
            context.user_data.pop("draft", None)
            await self.show_sale(update, context, int(parts[1]))
        elif name == "return" and len(parts) == 3:
            await self.begin_return(update, context, int(parts[1]), int(parts[2]))
        elif name in {"returnok", "voidok"} and len(parts) == 2:
            draft = context.user_data.get("draft", {})
            kind = "return" if name == "returnok" else "void"
            if draft.get("kind") != kind or draft.get("nonce") != parts[1] or draft.get("stage") != "ready":
                raise ValueError("Кнопка устарела или действие уже сохранено. Откройте /sales.")
            if kind == "return":
                sale = self.store.return_item(draft["sale_id"], draft["item_id"], draft["quantity"], self.actor(update), draft["nonce"], self.targets())
            else:
                sale = self.store.void_sale(draft["sale_id"], self.actor(update), self.targets())
            context.user_data.pop("draft", None)
            await update.effective_message.reply_text("Возврат сохранён ✅ Остатки восстановлены." if kind == "return" else "Продажа отменена ✅ Остатки восстановлены.")
            await self.show_sale(update, context, sale["id"])
        elif name == "voidask" and len(parts) == 2:
            sale = self.store.sale(int(parts[1]))
            if sale["status"] != "sold":
                raise ValueError("Продажа уже отменена.")
            draft = new_draft("void", "ready", sale_id=sale["id"])
            context.user_data["draft"] = draft
            await update.effective_message.reply_text(f"Отменить ошибочную продажу №{sale['id']}?\nВсе ещё не возвращённые пары вернутся в наличие.\nВ отчёте появится отмена на дату этой операции.", reply_markup=Keyboard([[Button("Да, отменить продажу", callback_data=f"voidok:{draft['nonce']}")], [Button("Назад", callback_data=f"sale:{sale['id']}")]]))
        elif name == "report" and len(parts) == 2:
            today = datetime.now(KYIV).date()
            period = parts[1]
            if period == "custom":
                context.user_data["draft"] = new_draft("report", "dates")
                await update.effective_message.reply_text("Введите начало и конец периода через пробел:\n01.10.2026 20.10.2026\n/cancel — отмена.")
            else:
                if period == "today":
                    first = last = today
                elif period == "yesterday":
                    first = last = today - timedelta(days=1)
                elif period == "week":
                    first, last = today - timedelta(days=today.weekday()), today
                elif period == "month":
                    first, last = today.replace(day=1), today
                else:
                    raise ValueError("Выберите период через /report.")
                await self.send_report(update, first, last)
        elif name == "section" and len(parts) == 3:
            section = context.user_data.get("sections", {})
            index = int(parts[2])
            if section.get("nonce") != parts[1] or not 0 <= index < len(section["categories"]):
                raise ValueError("Список разделов устарел. Откройте /sections.")
            context.user_data["catalog_filter"] = {"category": section["categories"][index]["category"]}
            await self.listing(update, context)
        elif name == "sections" and len(parts) == 2:
            await self.sections(update, context, int(parts[1]))
        elif parts == ["all_products"]:
            context.user_data.pop("catalog_filter", None)
            await self.listing(update, context)
        elif name == "qr" and len(parts) == 2:
            product = self.store.get(int(parts[1]))
            data = await asyncio.to_thread(qr_image, product.id, context.bot.username)
            await update.effective_message.reply_photo(data, caption=f"QR товара #{product.id} · {product.title}\nСканируйте камерой телефона или пришлите этот QR в /scan. Доступ к карточке есть только у сотрудников.")
        else:
            return False
        return True
