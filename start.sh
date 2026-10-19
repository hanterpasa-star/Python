#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

if [[ ! -f .env ]]; then
    cp .env.example .env
    printf '%s\n' 'Создан .env. Откройте его, заполните BOT_TOKEN и снова запустите bash start.sh.'
    exit 2
fi

exec .venv/bin/python -m shoe_shop_bot
