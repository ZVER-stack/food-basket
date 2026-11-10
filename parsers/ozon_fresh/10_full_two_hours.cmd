@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%FRESH_PY%" exit /b 1
echo Полный обход с общим бюджетом 7200 секунд, включая ожидания.
echo Проверенные SKU из прежнего снимка используются как помеченный справочник.
echo Завершение всех197адресов за это время пока не подтверждено.
echo Если адреса не закончены к сроку, результат останется неполным.
echo Интервал: сначала 5 секунд; после HTTP403/429 +1 секунда, максимум10.
echo Достигнутая ступень сохраняется при перезапуске. Ожидание доступа и Retry-After соблюдаются.
"%FRESH_PY%" "%~dp0run_parser.py" --action full --price-workers 7 --catalog-workers 7 --interval 5 --max-interval 10 --http-error-steps 6,7,8,9,10 --http-error-statuses 403,429 --price-policy catalog_metadata_cache --wait-on-403 --quarantine-incomplete
pause
