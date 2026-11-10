@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%FRESH_PY%" exit /b 1
echo Возобновление прежнего снимка без сброса защиты и без продления двух часов.
echo Общий справочник SKU; перенесённая цена явно помечается как непроверенная по адресу.
echo Интервал: сначала 5 секунд; после HTTP403/429 +1 секунда, максимум10.
echo Достигнутая ступень сохраняется при перезапуске. Ожидание доступа и Retry-After соблюдаются.
"%FRESH_PY%" "%~dp0run_parser.py" --action full --price-workers 7 --catalog-workers 7 --interval 5 --max-interval 10 --http-error-steps 6,7,8,9,10 --http-error-statuses 403,429 --resume-current --price-policy catalog_metadata_cache --wait-on-403 --quarantine-incomplete
pause
