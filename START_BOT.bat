@echo off
setlocal
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
    echo Created .env. Paste your BotFather token after BOT_TOKEN=.
    echo Save the file and close Notepad to continue.
    start /wait notepad.exe .env
)

".venv\Scripts\python.exe" -m shoe_shop_bot
if errorlevel 1 goto error
echo Bot stopped.
if /i not "%~1"=="--no-pause" pause
exit /b 0

:error
echo.
echo Bot could not start. Read the error above.
echo Check Python 3.10 or newer, and your .env settings.
echo Use EDIT_SETTINGS.bat to open the token settings in Notepad.
if /i not "%~1"=="--no-pause" pause
exit /b 1
