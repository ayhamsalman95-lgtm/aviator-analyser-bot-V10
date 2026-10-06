@echo off
cd /d "%~dp0"
set /p TOKEN=Paste your Telegram bot token: 
> .env echo TELEGRAM_BOT_TOKEN=%TOKEN%
echo Saved to .env (git-ignored).
pause
