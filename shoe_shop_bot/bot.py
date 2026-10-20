import asyncio
from contextlib import suppress
import logging
import secrets
import time

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.error import Conflict as TelegramConflict, Forbidden
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from .config import Settings
from .notifications import ACTION_NAMES, notification_loop
from .stock import changed_stock, format_stock, parse_size_list, parse_stock, validate_title
from .storage import Actor, Conflict, NoChange, NotFound, Product, Store
from .features import (BotFeatures, SELL_BUTTON, SEARCH_BUTTON, REPORT_BUTTON,
                       SECTIONS_BUTTON, SUMMARY_BUTTON, SALES_BUTTON, SCAN_BUTTON)
from .maintenance import maintenance_loop


log = logging.getLogger(__name__)
STOCK_BUTTON = "📦 В наличии"
EDIT_BUTTON = "✏️ Изменить наличие"
ADD_BUTTON = "➕ Добавить товар"
STAFF_BUTTON = "👥 Сотрудники"
PAGE_SIZE = 6


class ShopBot(BotFeatures):
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.started_at = time.monotonic()

    def allowed(self, user_id: int) -> bool:
        return self.settings.is_owner(user_id) or self.store.is_staff(user_id)

    def menu(self, user_id: int) -> ReplyKeyboardMarkup:
        rows = [[STOCK_BUTTON, SELL_BUTTON], [EDIT_BUTTON, ADD_BUTTON],
                [SEARCH_BUTTON, SECTIONS_BUTTON], [SALES_BUTTON, REPORT_BUTTON],
                [SUMMARY_BUTTON, SCAN_BUTTON]]
        if self.settings.is_owner(user_id):
            rows.append([STAFF_BUTTON])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    async def require_access(self, update: Update, context, *, owner=False, private=True) -> bool:
        user, chat, message = update.effective_user, update.effective_chat, update.effective_message
        if user is None or chat is None or message is None:
            return False
        allowed = self.settings.is_owner(user.id) if owner else self.allowed(user.id)
        if not allowed:
            context.user_data.pop("draft", None)
            if update.callback_query:
                await update.callback_query.answer("Доступ закрыт. Обратитесь к владельцу магазина.", show_alert=True)
            else:
                text = f"Это закрытый бот сотрудников.\nВаш Telegram ID: {user.id}\nНажмите «Запросить доступ» — владелец сможет разрешить его кнопкой."
                if owner and self.allowed(user.id):
                    text = "Эта команда доступна только владельцу магазина."
                await message.reply_text(text, reply_markup=ReplyKeyboardRemove())
                if not owner and chat.type == "private" and self.settings.owner_id:
                    await message.reply_text("Доступ выдаёт владелец магазина.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 Запросить доступ", callback_data="request_access")]]))
            return False
        if private and chat.type != "private":
            text = "Каталог и редактирование доступны только в личной переписке с ботом."
            if update.callback_query:
                await update.callback_query.answer(text, show_alert=True)
            else:
                await message.reply_text(text)
            return False
        return True

    def targets(self) -> list[int]:
        if self.settings.notification_mode == "staff":
            return sorted(set(self.store.staff_ids()) | ({self.settings.owner_id} if self.settings.owner_id else set()))
        chat = self.settings.notification_chat_id or self.store.notification_chat()
        return [chat] if chat else []

    def saved_text(self) -> str:
        if self.targets():
            return "Сохранено ✅ Уведомление поставлено в очередь отправки."
        return "Сохранено ✅\nУведомления пока не подключены. Владельцу нужно добавить бота в рабочую группу и вызвать там /notify_here."

    @staticmethod
    def actor(update: Update) -> Actor:
        return Actor(update.effective_user.id, update.effective_user.full_name)

    @staticmethod
    def cancel_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="cancel")]])

    @staticmethod
    def card(product: Product) -> str:
        return f"👟 {product.title}\nАртикул: #{product.id}\nРаздел: {product.category or 'Без раздела'}" + (f"\nКод: {product.code}" if product.code else "") + f"\n\nРазмер × количество пар:\n{format_stock(product.stock)}"

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.require_access(update, context):
            return
        context.user_data.pop("draft", None)
        context.user_data.pop("catalog_filter", None)
        if context.args and len(context.args) == 1 and context.args[0].startswith("p_"):
            try:
                return await self.scan_input(update, context, context.args[0])
            except (ValueError, NotFound):
                await update.effective_message.reply_text("Товар по этому QR-коду не найден. Откройте /stock.")
                return
        await update.effective_message.reply_text(
            "Магазин обуви 👟\n\n📦 В наличии — фото и размеры.\n💰 Продано — одна или несколько позиций, цена в гривнах.\n✏️ Изменить наличие — размеры, фото, раздел и код.\n➕ Добавить товар — новая модель.\n🔎 Поиск — название, размер, номер или код.\n🧾 Продажи и возвраты — посмотреть, вернуть или отменить.\n📊 Отчёты — продажи и выручка за период.\n\n/cancel — отменить ввод, /help — помощь.",
            reply_markup=self.menu(update.effective_user.id),
        )

    async def user_id(self, update: Update, context):
        if update.effective_user:
            await update.effective_message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")

    async def help(self, update: Update, context):
        if not await self.require_access(update, context):
            return
        text = (
            "/start — главное меню\n/stock — что есть в наличии\n/edit — изменить товары\n/add — добавить товар\n/history — последние изменения\n/cancel — отмена\n/id — ваш Telegram ID\n\n"
            "Размеры: 36, 37, 38.5 — по одной паре.\n36:2, 37:1 — две пары 36 и одна пара 37.\n"
            "«Добавить размеры» увеличивает количество.\n«Убрать размеры» убирает все пары указанных размеров.\n"
            "Чтобы уменьшить количество с 3 до 2, выберите «Задать всё наличие» и укажите новые остатки.\n"
            "«-» в полном наличии означает, что все размеры закончились."
        )
        text += ("\n\n/sell — новая продажа; /cart — текущая корзина\n/sales — продажи, возвраты и отмена\n/find — поиск\n/sections — разделы\n/scan — штрихкод или QR\n/summary — сводка остатков\n/report — отчёты и популярные модели / размеры\n"
                 "При продаже цена вводится за ОДНУ пару в гривнах. Остатки спишутся после подтверждения. Возврат учитывается по исходной цене.\n"
                 "Возвраты и отмены записываются в отчёт на дату операции.")
        if self.settings.is_owner(update.effective_user.id):
            text += "\n\n/staff — сотрудники\n/grant ID — выдать доступ\n/revoke ID — отозвать доступ\n/notify_here — подключить рабочую группу\n/status — состояние уведомлений"
            text += "\n/requests — запросы доступа\n/backup — получить резервную копию\n/report_time — время вечернего отчёта (Киев)"
        await update.effective_message.reply_text(text)

    async def listing(self, update: Update, context, mode="stock", page=0):
        if not await self.require_access(update, context):
            return
        context.user_data.pop("draft", None)
        selection = context.user_data.get("catalog_filter", {})
        selection = dict(selection, sort=context.user_data.get("catalog_sort", "title"))
        products, total = self.store.page(include_empty=mode == "edit", offset=page * PAGE_SIZE, limit=PAGE_SIZE, **selection)
        if not products and total and page:
            page = (total - 1) // PAGE_SIZE
            products, total = self.store.page(include_empty=mode == "edit", offset=page * PAGE_SIZE, limit=PAGE_SIZE, **selection)
        title = "📦 В наличии" if mode == "stock" else "✏️ Выберите товар для изменения"
        if selection.get("query") or "category" in selection:
            title += "\nФильтр: " + (selection.get("query") or selection.get("category") or "Без раздела")
        if not products:
            await update.effective_message.reply_text(title + ("\n\nНичего не найдено. /find — новый поиск, /stock — весь каталог." if selection.get("query") or "category" in selection else "\n\nСписок пуст. Добавьте товар через /add."))
            return
        rows = []
        for product in products:
            sizes = ", ".join(product.stock) or "нет в наличии"
            label = f"#{product.id} {product.title[:40]} · {sizes[:30]}"
            rows.append([InlineKeyboardButton(label, callback_data=f"view:{mode}:{product.id}")])
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("← Назад", callback_data=f"list:{mode}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            navigation.append(InlineKeyboardButton("Далее →", callback_data=f"list:{mode}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        if mode == "edit":
            rows.append([InlineKeyboardButton(ADD_BUTTON, callback_data="add")])
        rows.append([InlineKeyboardButton("↕️ Сортировка", callback_data=f"sortmenu:{mode}")])
        await update.effective_message.reply_text(
            f"{title}\nТоваров: {total}. Страница {page + 1}.\nПорядок: { {'title': 'по названию', 'newest': 'сначала новые', 'size': 'по размеру' }[selection['sort']]}\nНажмите на модель, чтобы открыть фото и размеры.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def stock_command(self, update: Update, context):
        context.user_data.pop("catalog_filter", None)
        await self.listing(update, context)

    async def edit_command(self, update: Update, context):
        context.user_data.pop("catalog_filter", None)
        await self.listing(update, context, "edit")

    async def show_product(self, update: Update, context, product_id: int, mode="stock"):
        product = self.store.get(product_id)
        if mode == "stock" and not product.stock:
            await update.effective_message.reply_text("Этого товара уже нет в наличии. Откройте актуальный список: /stock")
            return
        rows = []
        if mode == "edit":
            for label, action in [
                ("➕ Добавить размеры", "add_sizes"), ("➖ Убрать размеры", "remove_sizes"),
                ("✏️ Задать всё наличие", "set_sizes"), ("📷 Заменить фото", "photo"),
                ("✏️ Название", "rename"),
                ("🗂 Раздел", "category"), ("🏷 Код", "code"),
            ]:
                rows.append([InlineKeyboardButton(label, callback_data=f"edit:{action}:{product.id}:{product.version}")])
            rows.append([InlineKeyboardButton("🗑 Убрать товар из каталога", callback_data=f"archive:ask:{product.id}:{product.version}")])
        else:
            rows.append([InlineKeyboardButton(EDIT_BUTTON, callback_data=f"view:edit:{product.id}")])
        if product.stock:
            rows.append([InlineKeyboardButton(SELL_BUTTON, callback_data=f"sellp:{product.id}")])
        rows.append([InlineKeyboardButton("QR-код товара", callback_data=f"qr:{product.id}")])
        rows.append([InlineKeyboardButton("← К списку", callback_data=f"list:{mode}:0")])
        await update.effective_message.reply_photo(product.photo, caption=self.card(product), reply_markup=InlineKeyboardMarkup(rows))

    async def add(self, update: Update, context):
        if not await self.require_access(update, context):
            return
        context.user_data["draft"] = {"kind": "create", "stage": "title", "nonce": secrets.token_hex(6)}
        await update.effective_message.reply_text("Как называется модель? Например: Nike Air Force белые.\n/cancel — отмена.", reply_markup=self.cancel_keyboard())

    async def cancel(self, update: Update, context):
        if not await self.require_access(update, context):
            return
        context.user_data.pop("draft", None)
        await update.effective_message.reply_text("Ввод отменён. Несохранённые изменения не применены.", reply_markup=self.menu(update.effective_user.id))

    async def begin_edit(self, update: Update, context, action: str, product_id: int, version: int):
        product = self.store.get(product_id)
        if product.version != version:
            raise Conflict
        prompts = {
            "add_sizes": "Напишите, какие размеры добавить: 36, 37:2, 38.5.\nКоличество прибавится к текущему остатку.",
            "remove_sizes": "Напишите размеры, которые нужно полностью убрать: 36, 37.\nУберутся все пары этих размеров.",
            "set_sizes": "Напишите всё актуальное наличие: 36:2, 37, 38.5.\nЭто полностью заменит текущий список. «-» — всё закончилось.",
            "rename": "Напишите новое название модели (до 100 символов).",
            "photo": "Пришлите новое фото как фотографию, а не как файл.",
            "category": "Напишите раздел: например Nike, Adidas или Кроссовки. «-» — без раздела.",
            "code": "Необязательно: напишите штрихкод с коробки или пришлите его фото. Можно свой код: до 64 латинских букв, цифр, точек, дефисов или _. «-» — убрать код. Без кода товар тоже работает.",
        }
        if action not in prompts:
            raise ValueError("Неизвестная кнопка.")
        context.user_data["draft"] = {"kind": "edit", "stage": "input", "action": action, "product_id": product_id, "version": version, "nonce": secrets.token_hex(6)}
        await update.effective_message.reply_text(f"{product.title}\nСейчас: {format_stock(product.stock)}\n\n{prompts[action]}\n/cancel — отмена.", reply_markup=self.cancel_keyboard())

    async def message(self, update: Update, context):
        if not await self.require_access(update, context):
            return
        text = update.effective_message.text or ""
        if text == STOCK_BUTTON:
            return await self.stock_command(update, context)
        if text == EDIT_BUTTON:
            return await self.edit_command(update, context)
        if text == ADD_BUTTON:
            return await self.add(update, context)
        if text == STAFF_BUTTON:
            return await self.staff(update, context)
        handlers = {SELL_BUTTON: self.sell, SEARCH_BUTTON: self.find, REPORT_BUTTON: self.reports,
                    SECTIONS_BUTTON: self.sections, SUMMARY_BUTTON: self.summary,
                    SALES_BUTTON: self.sales, SCAN_BUTTON: self.scan}
        if text in handlers:
            return await handlers[text](update, context)
        draft = context.user_data.get("draft")
        if not draft:
            await update.effective_message.reply_text("Выберите действие в меню: /start")
            return
        try:
            if await self.feature_message(update, context, draft, text):
                return
            if draft["stage"] == "confirm":
                await update.effective_message.reply_text("Нажмите «Сохранить» под карточкой или /cancel.")
                return
            if draft["kind"] == "create":
                await self.create_input(update, context, draft, text)
            else:
                await self.edit_input(update, context, draft, text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc) + "\nПопробуйте ещё раз или /cancel.")
        except (NotFound, Conflict):
            context.user_data.pop("draft", None)
            await update.effective_message.reply_text("Товар уже изменён другим сотрудником или убран из каталога. Ваш ввод не сохранён. Откройте актуальную карточку: /edit")

    async def create_input(self, update, context, draft, text):
        if draft["stage"] == "title":
            draft["title"] = validate_title(text)
            draft["stage"] = "photo"
            await update.effective_message.reply_text("Пришлите одно фото модели как фотографию, а не как файл.")
        elif draft["stage"] == "photo":
            if not update.effective_message.photo:
                raise ValueError("Здесь нужно фото. В Telegram выберите отправку фотографии.")
            draft["photo"] = update.effective_message.photo[-1].file_id
            draft["stage"] = "stock"
            await update.effective_message.reply_text("Какие размеры есть?\nПример: 36, 37:2, 38.5.\nБез количества — одна пара; 37:2 — две пары размера 37.")
        elif draft["stage"] == "stock":
            draft["stock"] = parse_stock(text)
            duplicates = self.store.duplicates(draft["title"])
            warning = "⚠️ Уже есть модель с таким названием: " + ", ".join(f"#{p.id}" for p in duplicates[:5]) + ". Проверьте, нужен ли ещё один товар.\n\n" if duplicates else ""
            await update.effective_message.reply_photo(
                draft["photo"], caption=warning + f"Проверьте новый товар:\n{draft['title']}\nРазмер × количество пар:\n{format_stock(draft['stock'])}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Сохранить", callback_data=f"save:{draft['nonce']}")], [InlineKeyboardButton("Отмена", callback_data="cancel")]]),
            )
            draft["stage"] = "confirm"

    async def edit_input(self, update, context, draft, text):
        action = draft["action"]
        if action == "photo":
            if not update.effective_message.photo:
                raise ValueError("Пришлите фотографию, а не текст или файл.")
            value = update.effective_message.photo[-1].file_id
        elif action == "rename":
            value = validate_title(text)
        elif action == "category":
            value = self.store.validate_category(text)
        elif action == "code":
            if update.effective_message.photo:
                from .codes import decode_image
                file = await update.effective_message.photo[-1].get_file()
                data = await file.download_as_bytearray()
                text = (await asyncio.to_thread(decode_image, bytes(data)))[0]
            value = self.store.validate_code(text)
        elif action == "remove_sizes":
            value = parse_size_list(text)
        else:
            value = parse_stock(text)
        product = self.store.get(draft["product_id"])
        if product.version != draft["version"]:
            raise Conflict
        stock, title, photo = product.stock, product.title, product.photo
        if action in {"add_sizes", "remove_sizes", "set_sizes"}:
            stock = changed_stock(stock, action, value)
        elif action == "rename":
            title = value
        elif action == "photo":
            photo = value
        await update.effective_message.reply_photo(
            photo, caption=f"Проверьте изменения:\n{title}\nБыло: {format_stock(product.stock)}\nБудет: {format_stock(stock)}" + (f"\n{'Раздел' if action == 'category' else 'Код'}: {value or 'не задан'}" if action in {"category", "code"} else ""),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Сохранить", callback_data=f"save:{draft['nonce']}")], [InlineKeyboardButton("Отмена", callback_data="cancel")]]),
        )
        draft.update(value=value, stage="confirm")

    async def save(self, update, context, nonce: str):
        draft = context.user_data.get("draft")
        if not draft or draft["stage"] != "confirm" or draft["nonce"] != nonce:
            await update.effective_message.reply_text("Эта кнопка устарела или товар уже сохранён. Откройте /edit.")
            return
        if draft["kind"] == "create":
            product = self.store.create(draft["title"], draft["photo"], draft["stock"], self.actor(update), self.targets())
        else:
            product = self.store.change(draft["product_id"], draft["version"], draft["action"], draft["value"], self.actor(update), self.targets())
        context.user_data.pop("draft", None)
        await update.effective_message.reply_text(self.saved_text(), reply_markup=self.menu(update.effective_user.id))
        await self.show_product(update, context, product.id, "edit")

    async def callback(self, update: Update, context):
        if await self.access_callback(update, context):
            return
        if not await self.require_access(update, context):
            return
        query = update.callback_query
        await query.answer()
        parts = (query.data or "").split(":")
        try:
            if await self.feature_callback(update, context, parts):
                return
            elif parts == ["cancel"]:
                await self.cancel(update, context)
            elif parts == ["add"]:
                await self.add(update, context)
            elif len(parts) == 3 and parts[0] == "list" and parts[1] in {"stock", "edit"}:
                page = int(parts[2])
                if not 0 <= page <= 100000:
                    raise ValueError("Некорректная страница.")
                await self.listing(update, context, parts[1], page)
            elif len(parts) == 3 and parts[0] == "view" and parts[1] in {"stock", "edit"}:
                context.user_data.pop("draft", None)
                await self.show_product(update, context, int(parts[2]), parts[1])
            elif len(parts) == 4 and parts[0] == "edit":
                await self.begin_edit(update, context, parts[1], int(parts[2]), int(parts[3]))
            elif len(parts) == 2 and parts[0] == "save":
                await self.save(update, context, parts[1])
            elif len(parts) == 4 and parts[0] == "archive":
                product = self.store.get(int(parts[2]))
                if product.version != int(parts[3]):
                    raise Conflict
                if parts[1] == "ask":
                    context.user_data.pop("draft", None)
                    await update.effective_message.reply_text(f"Убрать «{product.title}» из каталога? История сохранится.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Да, убрать", callback_data=f"archive:confirm:{product.id}:{product.version}")], [InlineKeyboardButton("Отмена", callback_data="cancel")]]))
                elif parts[1] == "confirm":
                    self.store.change(product.id, product.version, "archive", None, self.actor(update), self.targets())
                    await update.effective_message.reply_text(self.saved_text())
                else:
                    raise ValueError("Неизвестная кнопка.")
            else:
                raise ValueError("Кнопка устарела. Откройте /start.")
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc) or "Не удалось применить действие. Откройте /start.")
        except OverflowError:
            await update.effective_message.reply_text("Некорректное число. Откройте /start.")
        except Conflict:
            context.user_data.pop("draft", None)
            await update.effective_message.reply_text("Другой сотрудник уже изменил этот товар. Ваши изменения не сохранены. Откройте свежую карточку через /edit; для продажи соберите корзину заново через /sell.")
        except NotFound:
            context.user_data.pop("draft", None)
            await update.effective_message.reply_text("Товар или продажа не найдены. Откройте актуальный список: /edit или /sales.")
        except NoChange:
            context.user_data.pop("draft", None)
            await update.effective_message.reply_text("Действие уже выполнено или данные совпадают. Ничего не изменилось. /edit — товары, /sales — продажи.")

    async def staff(self, update: Update, context):
        if not await self.require_access(update, context, owner=True):
            return
        context.user_data.pop("draft", None)
        ids = self.store.staff_ids()
        text = f"Владелец: {self.settings.owner_id}\nСотрудники:\n" + ("\n".join(map(str, ids)) or "Пока нет сотрудников.")
        text += "\n\nСотрудник может открыть /start и нажать «Запросить доступ». Вам придут кнопки «Разрешить» / «Отказать».\n/requests — необработанные запросы.\nВыдать доступ вручную: /grant 123456789\nОтозвать доступ: /revoke 123456789"
        # Split large staff lists at Telegram's message limit.
        for start in range(0, len(text), 4000):
            await update.effective_message.reply_text(text[start:start + 4000])

    async def access_change(self, update: Update, context, *, revoke=False):
        if not await self.require_access(update, context, owner=True):
            return
        try:
            if len(context.args) != 1 or not context.args[0].isascii() or not context.args[0].isdigit():
                raise ValueError
            user_id = int(context.args[0])
            if not 0 < user_id < 2**63:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text("Укажите числовой Telegram ID: " + ("/revoke" if revoke else "/grant") + " 123456789")
            return
        if self.settings.is_owner(user_id):
            await update.effective_message.reply_text("У владельца уже есть полный доступ. Его нельзя отозвать этой командой.")
            return
        if revoke:
            self.store.revoke(user_id)
            # Discard unfinished inputs without persisting personal conversations.
            if user_id in context.application.user_data:
                context.application.user_data[user_id].pop("draft", None)
            await update.effective_message.reply_text(f"Доступ для {user_id} отозван. Просмотр и изменение товаров закрыты.")
        else:
            self.store.grant(user_id)
            await update.effective_message.reply_text(f"Доступ для {user_id} выдан. Сотрудник может открыть /start и работать с товарами.")

    async def grant(self, update: Update, context):
        await self.access_change(update, context)

    async def revoke(self, update: Update, context):
        await self.access_change(update, context, revoke=True)

    async def notify_here(self, update: Update, context):
        if not await self.require_access(update, context, owner=True, private=False):
            return
        if update.effective_chat.type not in {"group", "supergroup"}:
            await update.effective_message.reply_text("Добавьте бота в закрытую рабочую группу и отправьте /notify_here в этой группе от своего аккаунта владельца.")
            return
        if self.settings.notification_mode != "group":
            await update.effective_message.reply_text("В настройках выбран режим личных уведомлений. Для группы задайте NOTIFICATION_MODE=group и перезапустите бота.")
            return
        if self.settings.notification_chat_id and self.settings.notification_chat_id != update.effective_chat.id:
            await update.effective_message.reply_text("В .env уже задана другая группа в NOTIFICATION_CHAT_ID. Очистите это поле и перезапустите бота, чтобы выбирать группу командой.")
            return
        self.store.set_notification_chat(update.effective_chat.id)
        await update.effective_message.reply_text("Группа подключена ✅ Здесь будут появляться новые товары и изменения с фото, размерами и именем сотрудника. Убедитесь, что в группе только сотрудники магазина.")

    async def history(self, update: Update, context):
        if not await self.require_access(update, context):
            return
        events = self.store.history(10)
        lines = []
        for event in events:
            stamp = event["created_at"][:16].replace("T", " ")
            lines.append(f"{stamp} UTC · {ACTION_NAMES[event['action']]}\n#{event['product_id']} {event['title'][:60]}\n{format_stock(event['stock'])[:120]}\n{event['actor_name'][:30]} (ID {event['actor_id']})")
        await update.effective_message.reply_text("\n\n".join(lines) if lines else "Изменений пока нет.")

    async def status(self, update: Update, context):
        return await self.status_features(update, context)



async def on_error(update, context):
    if isinstance(context.error, TelegramConflict):
        log.error("Другой процесс уже получает сообщения этого бота. Оставьте один запущенный экземпляр.")
    else:
        log.error("Ошибка обработки: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message and not isinstance(context.error, Forbidden):
        with suppress(Exception):
            await update.effective_message.reply_text("Не удалось завершить действие. Проверьте /edit или /history, прежде чем повторять сохранение. Если ошибка повторяется, сообщите владельцу.")


def build_application(settings: Settings, store: Store | None = None, *, request=None) -> Application:
    store = store or Store(settings.database_path)
    shop = ShopBot(settings, store)

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start", "Главное меню"), BotCommand("stock", "В наличии"),
            BotCommand("edit", "Изменить наличие"), BotCommand("add", "Добавить товар"),
            BotCommand("history", "Последние изменения"), BotCommand("id", "Мой Telegram ID"),
            BotCommand("cancel", "Отменить ввод"), BotCommand("help", "Помощь"),
            BotCommand("sell", "Новая продажа"), BotCommand("cart", "Корзина продажи"),
            BotCommand("sales", "Продажи и возвраты"), BotCommand("find", "Поиск"),
            BotCommand("sections", "Разделы"), BotCommand("scan", "Штрихкод / QR"),
            BotCommand("summary", "Остатки"), BotCommand("report", "Отчёты"),
            BotCommand("request_access", "Запросить доступ"),
        ])
        application.bot_data["notification_task"] = asyncio.create_task(notification_loop(application.bot, store, settings.owner_id))
        application.bot_data["maintenance_task"] = asyncio.create_task(maintenance_loop(store, settings.owner_id))
        log.info("Бот запущен. Откройте /start в Telegram.")
        if not settings.owner_id:
            log.warning("OWNER_ID пуст. Доступ закрыт всем: узнайте свой ID через /id, заполните OWNER_ID и перезапустите бота.")

    async def post_stop(application):
        for name in ("notification_task", "maintenance_task"):
            task = application.bot_data.get(name)
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    builder = Application.builder().token(settings.token).concurrent_updates(False).post_init(post_init).post_stop(post_stop)
    if request is not None:
        builder = builder.request(request)
    application = builder.build()
    for command, handler in [
        ("start", shop.start), ("stock", shop.stock_command), ("edit", shop.edit_command),
        ("add", shop.add), ("cancel", shop.cancel), ("id", shop.user_id),
        ("help", shop.help), ("staff", shop.staff), ("grant", shop.grant),
        ("revoke", shop.revoke), ("notify_here", shop.notify_here),
        ("history", shop.history), ("status", shop.status),
        ("sell", shop.sell), ("cart", shop.cart), ("sales", shop.sales),
        ("find", shop.find), ("sections", shop.sections), ("scan", shop.scan),
        ("summary", shop.summary), ("report", shop.reports),
        ("report_time", shop.report_time), ("backup", shop.backup_command),
        ("request_access", shop.request_access), ("requests", shop.requests),
    ]:
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(shop.callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, shop.message))
    application.add_error_handler(on_error)
    application.bot_data["shop"] = shop
    return application
