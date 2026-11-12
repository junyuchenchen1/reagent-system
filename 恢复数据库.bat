@echo off
chcp 65001 >nul
cd /d "%~dp0"
python restore.py
pause
