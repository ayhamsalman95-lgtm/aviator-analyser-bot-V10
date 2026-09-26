@echo off
cd /d "%~dp0"
start "Aviator collector" cmd /k run_collector.bat
start "Aviator Telegram bot" cmd /k run_bot.bat
