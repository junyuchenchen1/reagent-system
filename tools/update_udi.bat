@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_udi.ps1" %*
pause