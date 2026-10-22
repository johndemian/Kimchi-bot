@echo off
rem Launches the kimchi Telegram bot from its own virtual environment.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] creating virtual environment...
    py -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
    echo [error] could not create .venv - is Python installed? ^(py launcher^)
    exit /b 1
)

".venv\Scripts\python.exe" -c "import telegram" 2>nul
if errorlevel 1 (
    echo [setup] installing dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo [run] starting bot...
".venv\Scripts\python.exe" bot.py
