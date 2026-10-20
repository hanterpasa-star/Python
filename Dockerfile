FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/var/lib/shoe-shop-bot/shop.sqlite3

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && mkdir -p /var/lib/shoe-shop-bot \
    && chown 10001:10001 /var/lib/shoe-shop-bot

COPY shoe_shop_bot ./shoe_shop_bot
USER 10001:10001

CMD ["python", "-m", "shoe_shop_bot"]
