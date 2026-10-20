"""Persistent catalogue, sales ledger and durable service messages."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import secrets
import time

from .stock import MAX_QUANTITY, normalize_size, validate_stock


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class InventoryFeatures:
    def migrate(self):
        with self.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(products)")}
            legacy = "category" not in columns
            has_products = db.execute("SELECT 1 FROM products LIMIT 1").fetchone()
        # Take a consistent copy before upgrading a real, existing catalogue.
        if legacy and has_products:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            self.backup(self.path.parent / "backups" / f"before-upgrade-{stamp}.sqlite3")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(products)")}
            if "category" not in columns:
                db.execute("ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            # Leave obsolete columns in existing databases intact; no user data is dropped.
            # Statements are separate: executescript would commit a partial migration.
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("PRAGMA user_version=2")

    def setting(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.connect() as db:
            db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    @staticmethod
    def validate_category(value):
        value = " ".join(value.split())
        if value == "-":
            return ""
        if not 1 <= len(value) <= 50:
            raise ValueError("Название раздела: от 1 до 50 символов. «-» — без раздела.")
        return value

    def duplicates(self, title):
        key = " ".join(title.casefold().split())
        products, _ = self.page(include_empty=True, limit=1_000_000)
        return [p for p in products if " ".join(p.title.casefold().split()) == key]

    def categories(self):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT category,COUNT(*) AS models FROM products WHERE archived=0 GROUP BY category")]
        return sorted(rows, key=lambda r: r["category"].casefold())

    def summary(self):
        products, total = self.page(include_empty=True, limit=1_000_000)
        counts = Counter()
        for product in products:
            counts[product.category or "Без раздела"] += sum(product.stock.values())
        return {"models": total, "available": sum(bool(p.stock) for p in products),
                "pairs": sum(counts.values()), "categories": counts}

    def sell(self, items: list[dict], actor, nonce: str, targets=(), *, stamp=None):
        from .commerce import MAX_PRICE
        from .storage import Conflict, NotFound
        if not nonce or not 1 <= len(items) <= 20:
            raise ValueError("В продаже должно быть от 1 до 20 позиций.")
        stamp = stamp or utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,actor_id FROM sales WHERE nonce=?", (nonce,)).fetchone()
            if existing:
                if existing["actor_id"] != actor.id:
                    raise Conflict
                return self._sale(db, existing["id"])
            products, requested, checked = {}, Counter(), []
            for item in items:
                pid, qty, price = item["product_id"], item["quantity"], item["unit_price"]
                if type(qty) is not int or not 1 <= qty <= MAX_QUANTITY or type(price) is not int or not 0 < price <= MAX_PRICE:
                    raise ValueError("Проверьте количество и цену продажи.")
                size = normalize_size(item["size"])
                if pid not in products:
                    row = db.execute("SELECT * FROM products WHERE id=? AND archived=0", (pid,)).fetchone()
                    if row is None:
                        raise NotFound
                    products[pid] = self._product(row)
                product = products[pid]
                if product.version != item["version"]:
                    raise Conflict
                requested[pid, size] += qty
                if requested[pid, size] > product.stock.get(size, 0):
                    raise ValueError(f"Недостаточно пар: #{pid} {product.title}, размер {size}. Обновите корзину.")
                checked.append(dict(item, size=size))
            cursor = db.execute("INSERT INTO sales(created_at,actor_id,actor_name,nonce) VALUES (?,?,?,?)", (stamp, actor.id, actor.name[:60], nonce))
            sale_id = cursor.lastrowid
            for item in checked:
                pid = item["product_id"]
                cur = db.execute("INSERT INTO sale_items(sale_id,product_id,title,size,quantity,unit_price) VALUES (?,?,?,?,?,?)",
                                 (sale_id, pid, products[pid].title, item["size"], item["quantity"], item["unit_price"]))
                db.execute("INSERT INTO movements(sale_id,item_id,kind,quantity,amount,created_at) VALUES (?,?,?,?,?,?)",
                           (sale_id, cur.lastrowid, "sale", item["quantity"], item["quantity"] * item["unit_price"], stamp))
            for pid, old in products.items():
                stock = {size: qty - requested[pid, size] for size, qty in old.stock.items() if qty > requested[pid, size]}
                product = replace(old, stock=stock, version=old.version + 1)
                db.execute("UPDATE products SET stock=?,version=? WHERE id=?", (json.dumps(stock), product.version, pid))
                lines = [i for i in checked if i["product_id"] == pid]
                self._event(db, product, "sale", actor, old, targets, sale_id=sale_id, lines=lines,
                            amount=sum(i["quantity"] * i["unit_price"] for i in lines))
            return self._sale(db, sale_id)

    @staticmethod
    def _sale(db, sale_id):
        from .storage import NotFound
        row = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if row is None:
            raise NotFound
        sale = dict(row)
        sale["items"] = [dict(i) for i in db.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY id", (sale_id,))]
        sale["total"] = sum(i["quantity"] * i["unit_price"] for i in sale["items"])
        return sale

    def sale(self, sale_id):
        with self.connect() as db:
            return self._sale(db, sale_id)

    def sales_page(self, offset=0, limit=6):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM sales ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]
            return [self._sale(db, sid) for sid in ids], db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]

    def return_item(self, sale_id, item_id, quantity, actor, nonce, targets=(), *, stamp=None):
        from .storage import Conflict, NoChange
        if type(quantity) is not int or quantity <= 0 or not nonce:
            raise ValueError("Укажите положительное количество пар.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT sale_id,actor_id FROM returns WHERE nonce=?", (nonce,)).fetchone()
            if existing:
                if (existing["sale_id"], existing["actor_id"]) != (sale_id, actor.id):
                    raise Conflict
                return self._sale(db, sale_id)
            sale = self._sale(db, sale_id)
            item = next((i for i in sale["items"] if i["id"] == item_id), None)
            if sale["status"] != "sold" or item is None or quantity > item["quantity"] - item["returned"]:
                raise NoChange
            self._restore(db, item, quantity, "return", actor, targets, sale_id)
            stamp = stamp or utcnow()
            amount = quantity * item["unit_price"]
            db.execute("INSERT INTO returns(sale_id,item_id,quantity,amount,actor_id,nonce,created_at) VALUES (?,?,?,?,?,?,?)", (sale_id, item_id, quantity, amount, actor.id, nonce, stamp))
            db.execute("UPDATE sale_items SET returned=returned+? WHERE id=?", (quantity, item_id))
            db.execute("INSERT INTO movements(sale_id,item_id,kind,quantity,amount,created_at) VALUES (?,?,?,?,?,?)", (sale_id, item_id, "return", -quantity, -amount, stamp))
            return self._sale(db, sale_id)

    def _restore(self, db, item, quantity, action, actor, targets, sale_id):
        row = db.execute("SELECT * FROM products WHERE id=?", (item["product_id"],)).fetchone()
        old = self._product(row)
        stock = dict(old.stock)
        stock[item["size"]] = stock.get(item["size"], 0) + quantity
        stock = validate_stock(stock)
        product = replace(old, stock=stock, version=old.version + 1, archived=False)
        db.execute("UPDATE products SET stock=?,version=?,archived=0 WHERE id=?", (json.dumps(stock), product.version, old.id))
        self._event(db, product, action, actor, old, targets, sale_id=sale_id,
                    lines=[dict(item, quantity=quantity)], amount=-quantity * item["unit_price"])

    def void_sale(self, sale_id, actor, targets=(), *, stamp=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sale = self._sale(db, sale_id)
            if sale["status"] == "void":
                return sale
            stamp = stamp or utcnow()
            for item in sale["items"]:
                quantity = item["quantity"] - item["returned"]
                if quantity:
                    self._restore(db, item, quantity, "void", actor, targets, sale_id)
                    db.execute("INSERT INTO movements(sale_id,item_id,kind,quantity,amount,created_at) VALUES (?,?,?,?,?,?)", (sale_id, item["id"], "void", -quantity, -quantity * item["unit_price"], stamp))
            db.execute("UPDATE sales SET status='void',voided_at=?,voided_by=? WHERE id=?", (stamp, actor.id, sale_id))
            return self._sale(db, sale_id)

    def report(self, start, end):
        with self.connect() as db:
            rows = db.execute("""SELECT m.*,i.product_id,i.title,i.size FROM movements m
                JOIN sale_items i ON i.id=m.item_id WHERE m.created_at>=? AND m.created_at<? ORDER BY m.id""", (start, end)).fetchall()
        models, sizes = Counter(), Counter()
        titles = {}
        for row in rows:
            models[row["product_id"]] += row["quantity"]
            titles[row["product_id"]] = row["title"]
            sizes[row["size"]] += row["quantity"]
        return {"sold": sum(r["quantity"] for r in rows if r["kind"] == "sale"),
                "returned": -sum(r["quantity"] for r in rows if r["kind"] == "return"),
                "voided": -sum(r["quantity"] for r in rows if r["kind"] == "void"),
                "gross": sum(r["amount"] for r in rows if r["kind"] == "sale"),
                "deductions": -sum(r["amount"] for r in rows if r["kind"] != "sale"),
                "net_quantity": sum(r["quantity"] for r in rows), "net_amount": sum(r["amount"] for r in rows),
                "models": [dict(product_id=pid, title=titles[pid], quantity=q) for pid, q in models.most_common(5) if q > 0],
                "sizes": [dict(size=size, quantity=q) for size, q in sizes.most_common(5) if q > 0]}

    @staticmethod
    def _notice(db, key, chat_id, text, markup=None):
        db.execute("INSERT OR IGNORE INTO notices(key,chat_id,text,markup) VALUES (?,?,?,?)", (key, chat_id, text, json.dumps(markup) if markup else None))

    def notice(self, key, chat_id, text, markup=None):
        with self.connect() as db:
            self._notice(db, key, chat_id, text, markup)

    def pending_notices(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM notices WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT 20", (time.time(),))]

    def notice_result(self, notice_id, status="sent", delay=0):
        with self.connect() as db:
            db.execute("UPDATE notices SET status=?,attempts=attempts+1,next_attempt=? WHERE id=?", (status, time.time() + delay, notice_id))

    def request_access(self, user_id, name, owner_id):
        if owner_id <= 0 or user_id <= 0:
            raise ValueError("Владелец ещё не настроил доступ. Попробуйте позже.")
        now = datetime.now(timezone.utc)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM access_requests WHERE user_id=?", (user_id,)).fetchone()
            if row and (row["status"] == "pending" or now - datetime.fromisoformat(row["created_at"]) < timedelta(hours=24)):
                return False
            nonce = secrets.token_hex(8)
            db.execute("INSERT INTO access_requests(user_id,name,status,nonce,created_at) VALUES (?,?,'pending',?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,status='pending',nonce=excluded.nonce,created_at=excluded.created_at", (user_id, name[:100], nonce, now.isoformat()))
            markup = [[{"text": "✅ Разрешить", "callback_data": f"access:yes:{user_id}:{nonce}"}, {"text": "❌ Отказать", "callback_data": f"access:no:{user_id}:{nonce}"}]]
            self._notice(db, f"access:{nonce}", owner_id, f"👤 Запрос доступа\n{name[:100]}\nTelegram ID: {user_id}\nРазрешайте доступ только своим сотрудникам.", markup)
            return True

    def access_requests(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM access_requests WHERE status='pending' ORDER BY created_at LIMIT 30")]

    def decide_access(self, user_id, nonce, approve):
        from .storage import NoChange
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM access_requests WHERE user_id=? AND nonce=? AND status='pending'", (user_id, nonce)).fetchone()
            if row is None:
                raise NoChange
            db.execute("UPDATE access_requests SET status=? WHERE user_id=?", ("approved" if approve else "denied", user_id))
            if approve:
                db.execute("INSERT OR IGNORE INTO staff(user_id,added_at) VALUES (?,?)", (user_id, utcnow()))


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,
        actor_id INTEGER NOT NULL,actor_name TEXT NOT NULL,nonce TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'sold',voided_at TEXT,voided_by INTEGER)""",
    """CREATE TABLE IF NOT EXISTS sale_items(id INTEGER PRIMARY KEY AUTOINCREMENT,sale_id INTEGER NOT NULL REFERENCES sales(id),
        product_id INTEGER NOT NULL REFERENCES products(id),title TEXT NOT NULL,size TEXT NOT NULL,
        quantity INTEGER NOT NULL,unit_price INTEGER NOT NULL,returned INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY AUTOINCREMENT,sale_id INTEGER NOT NULL REFERENCES sales(id),
        item_id INTEGER NOT NULL REFERENCES sale_items(id),kind TEXT NOT NULL,quantity INTEGER NOT NULL,amount INTEGER NOT NULL,created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS movement_date ON movements(created_at)",
    """CREATE TABLE IF NOT EXISTS returns(id INTEGER PRIMARY KEY AUTOINCREMENT,sale_id INTEGER NOT NULL REFERENCES sales(id),
        item_id INTEGER NOT NULL REFERENCES sale_items(id),quantity INTEGER NOT NULL,amount INTEGER NOT NULL,actor_id INTEGER NOT NULL,
        nonce TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT NOT NULL UNIQUE,chat_id INTEGER NOT NULL,
        text TEXT NOT NULL,markup TEXT,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS access_requests(user_id INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,
        nonce TEXT NOT NULL,created_at TEXT NOT NULL)""",
]
