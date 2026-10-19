from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from .stock import changed_stock, validate_stock, validate_title


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class NoChange(Exception):
    pass


@dataclass(frozen=True)
class Product:
    id: int
    title: str
    photo: str
    stock: dict[str, int]
    version: int
    archived: bool = False


@dataclass(frozen=True)
class Actor:
    id: int
    name: str


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    photo TEXT NOT NULL,
                    stock TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (event_id, chat_id)
                );
                CREATE INDEX IF NOT EXISTS pending_deliveries
                    ON deliveries(status, next_attempt);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS staff (
                    user_id INTEGER PRIMARY KEY,
                    added_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _product(row: sqlite3.Row) -> Product:
        return Product(row["id"], row["title"], row["photo"], json.loads(row["stock"]), row["version"], bool(row["archived"]))

    def get(self, product_id: int) -> Product:
        with self.connect() as db:
            row = db.execute("SELECT * FROM products WHERE id=? AND archived=0", (product_id,)).fetchone()
        if row is None:
            raise NotFound
        return self._product(row)

    def page(self, *, include_empty: bool = False, offset: int = 0, limit: int = 6) -> tuple[list[Product], int]:
        where = "archived=0" + ("" if include_empty else " AND stock <> '{}'")
        with self.connect() as db:
            count = db.execute(f"SELECT COUNT(*) FROM products WHERE {where}").fetchone()[0]
            rows = db.execute(f"SELECT * FROM products WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", (limit, max(0, offset))).fetchall()
        return [self._product(row) for row in rows], count

    @staticmethod
    def _event(db, product: Product, action: str, actor: Actor, before: Product | None, targets) -> int:
        payload = {
            "product_id": product.id, "title": product.title, "photo": product.photo,
            "stock": product.stock, "action": action,
            "actor_id": actor.id, "actor_name": " ".join(actor.name.split())[:60],
            "before_stock": before.stock if before else None,
            "before_title": before.title if before else None,
        }
        cursor = db.execute(
            "INSERT INTO events(product_id,payload,created_at) VALUES (?,?,?)",
            (product.id, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        event_id = cursor.lastrowid
        db.executemany("INSERT INTO deliveries(event_id,chat_id) VALUES (?,?)", [(event_id, chat) for chat in set(targets)])
        return event_id

    def create(self, title: str, photo: str, stock: dict[str, int], actor: Actor, targets=()) -> Product:
        title, stock = validate_title(title), validate_stock(stock)
        if not photo:
            raise ValueError("Нужно прикрепить фотографию товара.")
        with self.connect() as db:
            cursor = db.execute("INSERT INTO products(title,photo,stock) VALUES (?,?,?)", (title, photo, json.dumps(stock)))
            product = Product(cursor.lastrowid, title, photo, stock, 1)
            self._event(db, product, "create", actor, None, targets)
        return product

    def change(self, product_id: int, version: int, action: str, value, actor: Actor, targets=()) -> Product:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM products WHERE id=? AND archived=0", (product_id,)).fetchone()
            if row is None:
                raise NotFound
            old = self._product(row)
            if old.version != version:
                raise Conflict
            title, photo, stock, archived = old.title, old.photo, old.stock, False
            if action in {"add_sizes", "remove_sizes", "set_sizes"}:
                stock = changed_stock(stock, action, value)
            elif action == "rename":
                title = validate_title(value)
            elif action == "photo":
                if not isinstance(value, str) or not value:
                    raise ValueError("Нужно прикрепить фотографию.")
                photo = value
            elif action == "archive":
                archived = True
            else:
                raise ValueError("Неизвестное действие.")
            if (title, photo, stock, archived) == (old.title, old.photo, old.stock, old.archived):
                raise NoChange
            product = Product(old.id, title, photo, stock, old.version + 1, archived)
            db.execute("UPDATE products SET title=?,photo=?,stock=?,version=?,archived=? WHERE id=?", (title, photo, json.dumps(stock), product.version, int(archived), old.id))
            self._event(db, product, action, actor, old, targets)
        return product

    def set_notification_chat(self, chat_id: int):
        with self.connect() as db:
            db.execute("INSERT INTO settings(key,value) VALUES ('notification_chat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(chat_id),))
            db.execute("UPDATE deliveries SET status='cancelled' WHERE chat_id<0 AND chat_id<>? AND status='pending'", (chat_id,))

    def notification_chat(self) -> int | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='notification_chat'").fetchone()
        return int(row[0]) if row else None

    def history(self, limit: int = 10) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,payload,created_at FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(json.loads(row["payload"]), event_id=row["id"], created_at=row["created_at"]) for row in rows]

    def pending(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT d.*,e.payload FROM deliveries d JOIN events e ON e.id=d.event_id
                WHERE d.status='pending' AND d.next_attempt <= ?
                AND NOT EXISTS (
                    SELECT 1 FROM deliveries older
                    WHERE older.chat_id=d.chat_id AND older.event_id<d.event_id
                    AND older.status='pending'
                ) ORDER BY d.event_id LIMIT ?""", (time.time(), limit)).fetchall()
        return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def delivery_result(self, event_id: int, chat_id: int, *, status: str = "sent", delay: float = 0):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status=?,attempts=attempts+1,next_attempt=? WHERE event_id=? AND chat_id=?", (status, time.time() + delay, event_id, chat_id))

    def delivery_counts(self) -> dict[str, int]:
        with self.connect() as db:
            return dict(db.execute("SELECT status,COUNT(*) FROM deliveries GROUP BY status").fetchall())

    def is_staff(self, user_id: int) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM staff WHERE user_id=?", (user_id,)).fetchone() is not None

    def staff_ids(self) -> list[int]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT user_id FROM staff ORDER BY added_at,user_id")]

    def grant(self, user_id: int):
        if user_id <= 0:
            raise ValueError("Нужен положительный числовой ID пользователя Telegram.")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO staff(user_id,added_at) VALUES (?,?)", (user_id, datetime.now(timezone.utc).isoformat()))

    def revoke(self, user_id: int):
        with self.connect() as db:
            db.execute("DELETE FROM staff WHERE user_id=?", (user_id,))
            db.execute("UPDATE deliveries SET status='cancelled' WHERE chat_id=? AND status='pending'", (user_id,))

    def backup(self, destination: Path):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError("Резервная копия должна быть в другом файле.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError("Файл копии уже существует. Выберите новое имя.")
        # SQLite backup includes committed WAL contents and is safe while the bot runs.
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
