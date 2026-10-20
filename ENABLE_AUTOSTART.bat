@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows-autostart.ps1"
if errorlevel 1 (
    echo Failed. Read the error above.
    pause
    exit /b 1
)
pause
