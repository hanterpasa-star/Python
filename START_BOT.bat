@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto error

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Создан файл .env. Впишите токен из BotFather после BOT_TOKEN=.
    echo Сохраните файл и закройте Блокнот.
    start /wait notepad.exe .env
)

".venv\Scripts\python.exe" -m shoe_shop_bot
if errorlevel 1 goto error
echo Бот остановлен.
pause
exit /b 0

:error
echo.
echo Не удалось запустить бота. Прочитайте сообщение выше.
echo Проверьте Python версии 3.10 или новее и настройки в .env.
pause
exit /b 1
