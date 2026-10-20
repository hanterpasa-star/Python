from io import BytesIO
import re
from urllib.parse import parse_qs, urlparse
import warnings

import qrcode
from PIL import Image, UnidentifiedImageError
import zxingcpp


def product_reference(text: str, username: str) -> tuple[str, str]:
    text = text.strip()
    if re.fullmatch(r"p_[1-9][0-9]{0,15}", text):
        return "id", text[2:]
    if text.startswith("p_"):
        raise ValueError("Некорректный номер товара в QR-коде.")
    if text.startswith("https://t.me/"):
        parsed = urlparse(text)
        if parsed.netloc == "t.me" and parsed.path.strip("/").casefold() == username.casefold():
            value = parse_qs(parsed.query).get("start", [""])[0]
            if re.fullmatch(r"p_[1-9][0-9]{0,15}", value):
                return "id", value[2:]
            raise ValueError("В QR-коде нет корректного номера товара.")
        raise ValueError("Этот QR-код относится к другому боту.")
    if re.fullmatch(r"#[1-9][0-9]{0,15}", text):
        return "id", text[1:]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", text):
        raise ValueError("Пришлите код товара, например 4820012345678, или фото штрихкода / QR.")
    return "code", text


def qr_image(product_id: int, username: str) -> BytesIO:
    image = qrcode.make(f"https://t.me/{username}?start=p_{product_id}")
    stream = BytesIO()
    stream.name = f"product-{product_id}.png"
    image.save(stream, format="PNG")
    stream.seek(0)
    return stream


def decode_image(data: bytes) -> list[str]:
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("Фото слишком большое. Пришлите фото до 10 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.width * source.height > 20_000_000:
                    raise ValueError("Пришлите фото меньшего размера (до 20 мегапикселей).")
                image = source.convert("RGB")
            codes = list(dict.fromkeys(result.text for result in zxingcpp.read_barcodes(image) if result.valid))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Не удалось прочитать изображение. Пришлите обычную фотографию кода.") from None
    if not codes:
        raise ValueError("Код не найден. Сфотографируйте его ближе, без бликов, или введите цифры вручную.")
    if len(codes) > 1:
        raise ValueError("На фото несколько кодов. Пришлите один нужный код.")
    return codes
