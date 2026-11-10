@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%FRESH_PY%" exit /b 1
echo Основной сбор всех197адресов: начать или продолжить сохранённый снимок.
echo Неполные товары сохраняются отдельно.
echo При капче пройдите её вручную и запустите06: работающий сбор получит подтверждение.
echo При Retry-After или ручной проверке ожидание может быть дольше 15 секунд.
echo Ctrl+C сохраняет прогресс. Проверка состояния: 12_status.cmd.
echo Интервал: сначала 5 секунд; после HTTP403/429 +1 секунда, максимум10.
echo Достигнутая ступень сохраняется при перезапуске. Ожидание доступа и Retry-After соблюдаются.
"%FRESH_PY%" "%~dp0ozon_repair_counter.py"
if errorlevel 1 (
  pause
  exit /b 1
)
"%FRESH_PY%" "%~dp0run_parser.py" --action full --price-workers 7 --catalog-workers 7 --interval 5 --max-interval 10 --http-error-steps 6,7,8,9,10 --http-error-statuses 403,429 --price-policy catalog_metadata_cache --continue-until-finished --wait-on-403 --quarantine-incomplete
pause
