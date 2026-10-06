@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/4] Creating virtual environment (.venv)...
if not exist .venv\Scripts\python.exe py -3 -m venv .venv || python -m venv .venv
echo [2/4] Installing pinned requirements...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (echo pip install failed & pause & exit /b 1)
.venv\Scripts\python.exe -m pip freeze > requirements.lock.txt
echo [3/4] Checking dependencies (sfs2x-py API, telegram, playwright)...
.venv\Scripts\python.exe scripts\check_env.py
echo [4/4] Migrating legacy data if present (idempotent, never deletes)...
.venv\Scripts\python.exe scripts\migrate_legacy.py
echo.
echo Done. Set TELEGRAM_BOT_TOKEN (see README_AR.md), then run run_all.bat
pause
