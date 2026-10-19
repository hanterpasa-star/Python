@echo off
setlocal
cd /d "%~dp0"

if exist ".env" goto open_settings
copy ".env.example" ".env" >nul
if errorlevel 1 goto error

:open_settings
start "" notepad.exe "%CD%\.env"
exit /b 0

:error
echo Could not create .env. Extract the complete ZIP into a regular folder first.
pause
exit /b 1
