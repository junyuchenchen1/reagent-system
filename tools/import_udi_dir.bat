@echo off
setlocal
cd /d "%~dp0.."
set "PY=E:\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0import_udi_dir.py" %*
echo.
pause