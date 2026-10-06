@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Aviator collector
.venv\Scripts\python.exe collector.py
pause
