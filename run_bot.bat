@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Aviator Telegram bot
if "%TELEGRAM_BOT_TOKEN%"=="" if not exist .env (echo TELEGRAM_BOT_TOKEN is not set. See README_AR.md & pause & exit /b 1)
.venv\Scripts\python.exe bot.py
pause
