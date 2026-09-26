@echo off
rem V11 installer referenced a missing apply_v11_patch.py. It now redirects to install.bat.
cd /d "%~dp0"
call install.bat
