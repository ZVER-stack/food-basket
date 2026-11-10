@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%FRESH_PY%" (
  echo Не найден Python проекта.
  pause
  exit /b 1
)
"%FRESH_PY%" -u "%~dp0ozon_watch.py"
pause
