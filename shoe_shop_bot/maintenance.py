import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
import secrets

from .commerce import KYIV, date_bounds, report_text

log = logging.getLogger(__name__)


def make_backup(store, *, now=None) -> Path:
    now = now or datetime.now(timezone.utc)
    directory = store.path.parent / "backups"
    name = f"auto-{now:%Y%m%dT%H%M%S}-{secrets.token_hex(4)}.sqlite3"
    destination = directory / name
    temporary = destination.with_suffix(".partial")
    try:
        store.backup(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    store.set_setting("last_backup_time", now.isoformat())
    store.set_setting("last_backup_file", str(destination))
    # Only rotate copies made by this function. Never remove manual/imported files.
    copies = sorted(directory.glob("auto-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in copies[28:]:
        old.unlink()
    return destination


def schedule_reports(store, owner_id, now):
    if owner_id <= 0:
        return
    local = now.astimezone(KYIV)
    today = local.date()
    value = store.setting("report_time", "21:00")
    if value == "off":
        store.set_setting("last_report_day", (today - timedelta(days=1)).isoformat())
        return
    due = today if local.strftime("%H:%M") >= value else today - timedelta(days=1)
    previous = store.setting("last_report_day")
    if previous is None:
        # A new installation starts with today's report, not historical empty reports.
        previous = (today - timedelta(days=1)).isoformat()
        store.set_setting("last_report_day", previous)
    cursor = date.fromisoformat(previous) + timedelta(days=1)
    for _ in range(7):
        if cursor > due:
            break
        text = "🌙 Вечерний отчёт\n" + report_text(store.report(*date_bounds(cursor, cursor)), cursor, cursor)
        if cursor == today:
            text += f"\nОперации до {local:%H:%M} по Киеву; полный день доступен завтра в /report."
        # Queue and advance together. The unique key also survives a clock rollback.
        with store.connect() as db:
            store._notice(db, f"report:{cursor.isoformat()}:{owner_id}", owner_id, text)
            db.execute("INSERT INTO settings(key,value) VALUES ('last_report_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cursor.isoformat(),))
        cursor += timedelta(days=1)


def maintenance_tick(store, owner_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = store.setting("last_backup_time")
    file = store.setting("last_backup_file")
    if not stamp or not file or not Path(file).is_file() or now - datetime.fromisoformat(stamp) >= timedelta(hours=6):
        make_backup(store, now=now)
    schedule_reports(store, owner_id, now)


async def maintenance_loop(store, owner_id):
    while True:
        work = asyncio.create_task(asyncio.to_thread(maintenance_tick, store, owner_id))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # Finish the current online copy before shutting down the application.
            await work
            raise
        except Exception as exc:
            log.error("Ошибка резервной копии или отчёта: %s. Проверьте свободное место и /status.", type(exc).__name__)
        await asyncio.sleep(30)
