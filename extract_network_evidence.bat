@echo off
REM Extract network evidence from game_network.jsonl
REM Usage: extract_network_evidence.bat <path_to_jsonl> [--output DERIVED.jsonl]

setlocal enabledelayedexpansion

if "%1"=="" (
    echo Usage: extract_network_evidence.bat ^<path_to_jsonl^> [--output DERIVED.jsonl]
    echo.
    echo Example:
    echo   extract_network_evidence.bat D:\game_network.jsonl --output D:\derived.jsonl
    exit /b 1
)

python tools\extract_network_evidence.py %*
