"""Money, dates and reports. Amounts are stored as integer kopecks."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
MAX_PRICE = 1_000_000_000


def parse_money(text: str) -> int:
    text = text.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", text):
        raise ValueError("Введите цену за одну пару в гривнах: например 1500 или 1500,50.")
    try:
        value = int(Decimal(text) * 100)
    except (InvalidOperation, OverflowError):
        raise ValueError("Не удалось прочитать цену.") from None
    if not 0 < value <= MAX_PRICE:
        raise ValueError("Цена должна быть больше 0 и не больше 10 000 000 грн.")
    return value


def money(amount: int) -> str:
    sign = "−" if amount < 0 else ""
    whole, fraction = divmod(abs(amount), 100)
    return f"{sign}{whole:,}".replace(",", " ") + f",{fraction:02d} грн"


def parse_date(text: str) -> date:
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    raise ValueError("Дата: ДД.ММ.ГГГГ, например 20.10.2026.")


def date_bounds(first: date, last: date) -> tuple[str, str]:
    if first > last or (last - first).days > 3660:
        raise ValueError("Начало должно быть не позже конца. Максимальный период — 10 лет.")
    return (datetime.combine(first, time.min, KYIV).astimezone(timezone.utc).isoformat(),
            datetime.combine(last + timedelta(days=1), time.min, KYIV).astimezone(timezone.utc).isoformat())


def report_text(report: dict, first: date, last: date) -> str:
    lines = [f"📊 Отчёт {first:%d.%m.%Y} — {last:%d.%m.%Y} (Киев)",
             f"Продано пар: {report['sold']}", f"Возвращено: {report['returned']}",
             f"Отменено пар: {report['voided']}", f"Итого пар: {report['net_quantity']}",
             f"Выручка: {money(report['gross'])}",
             f"Возвраты и отмены: {money(report['deductions'])}",
             f"Итого выручка: {money(report['net_amount'])}",
             "\nПопулярные модели (с учётом возвратов):"]
    lines += [f"#{row['product_id']} {row['title'][:55]} — {row['quantity']} пар" for row in report['models']]
    if not report['models']:
        lines.append("Продаж пока нет.")
    lines.append("\nПопулярные размеры:")
    lines += [f"{row['size']} — {row['quantity']} пар" for row in report['sizes']]
    lines.append("\nВозвраты и отмены учитываются в день операции. Суммы — в UAH.")
    return "\n".join(lines)
