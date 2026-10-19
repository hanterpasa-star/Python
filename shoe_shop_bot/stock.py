from decimal import Decimal, InvalidOperation
import re


MAX_SIZES = 25
MAX_QUANTITY = 9999


def normalize_size(value: str) -> str:
    if not re.fullmatch(r"\d{1,2}(?:\.\d)?", value):
        raise ValueError("Размер должен быть числом, например 38 или 38.5.")
    try:
        size = Decimal(value)
    except InvalidOperation:
        raise ValueError("Не удалось прочитать размер.") from None
    if not 0 < size < 100:
        raise ValueError("Размер должен быть больше 0 и меньше 100.")
    return format(size.normalize(), "f")


def validate_stock(stock: dict[str, int]) -> dict[str, int]:
    if len(stock) > MAX_SIZES:
        raise ValueError(f"Можно указать максимум {MAX_SIZES} разных размеров.")
    result = {}
    for raw_size, quantity in stock.items():
        size = normalize_size(raw_size)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
            raise ValueError(f"Количество пар должно быть от 1 до {MAX_QUANTITY}.")
        if size in result:
            raise ValueError(f"Размер {size} указан дважды.")
        result[size] = quantity
    return dict(sorted(result.items(), key=lambda item: Decimal(item[0])))


def parse_stock(text: str) -> dict[str, int]:
    """'36, 37:2, 38.5' -> sizes with pair counts; '-' clears stock."""
    text = text.strip()
    if text.lower() in {"-", "нет", "0"}:
        return {}
    if not text:
        raise ValueError("Напишите размеры, например: 36, 37:2, 38.5.")
    # Allow whitespace around a quantity separator without splitting the item.
    text = re.sub(r"\s*:\s*", ":", text)
    result = {}
    for item in re.split(r"[,;\s]+", text):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) > 2:
            raise ValueError("Формат количества: 38:2 — две пары размера 38.")
        size = normalize_size(parts[0])
        if size in result:
            raise ValueError(f"Размер {size} указан дважды. Используйте {size}:2 для двух пар.")
        if len(parts) == 2:
            if not re.fullmatch(r"[0-9]{1,4}", parts[1]):
                raise ValueError("Количество пар должно быть целым числом от 1 до 9999.")
            quantity = int(parts[1])
        else:
            quantity = 1
        result[size] = quantity
    if not result:
        raise ValueError("Напишите хотя бы один размер или «-» для пустого наличия.")
    return validate_stock(result)


def parse_size_list(text: str) -> list[str]:
    if ":" in text:
        raise ValueError("Здесь нужны только размеры: 36, 37. Убираются все пары этих размеров.")
    stock = parse_stock(text)
    if not stock:
        raise ValueError("Укажите размеры, которые нужно убрать, например 36, 37.")
    return list(stock)


def changed_stock(stock: dict[str, int], action: str, value) -> dict[str, int]:
    result = dict(stock)
    if action == "set_sizes":
        result = dict(value)
    elif action == "add_sizes":
        if not value:
            raise ValueError("Укажите хотя бы один размер для добавления.")
        for size, quantity in validate_stock(value).items():
            result[size] = result.get(size, 0) + quantity
    elif action == "remove_sizes":
        for size in value:
            result.pop(normalize_size(size), None)
    else:
        raise ValueError("Неизвестное действие с размерами.")
    return validate_stock(result)


def format_stock(stock: dict[str, int]) -> str:
    if not stock:
        return "нет в наличии"
    return ", ".join(f"{size} × {quantity}" for size, quantity in validate_stock(stock).items())


def validate_title(title: str) -> str:
    title = " ".join(title.split())
    if not 1 <= len(title) <= 100:
        raise ValueError("Название должно содержать от 1 до 100 символов.")
    return title
