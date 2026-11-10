@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%FRESH_PY%" exit /b 1
echo Сначала пройдите капчу вручную в прежнем Chrome и откройте товар с ценами.
echo Сохранённая увеличенная пауза выдерживается автоматически; файл06 её не сокращает.
echo Этот запуск подтверждает вашу ручную проверку и возобновляет тот же снимок.
echo Интервал: сначала 5 секунд; после HTTP403/429 +1 секунда, максимум10.
echo Достигнутая ступень сохраняется при перезапуске. Ожидание доступа и Retry-After соблюдаются.
"%FRESH_PY%" "%~dp0run_parser.py" --action full --price-workers 7 --catalog-workers 7 --interval 5 --max-interval 10 --http-error-steps 6,7,8,9,10 --http-error-statuses 403,429 --resume-after-manual-check --price-policy catalog_metadata_cache --continue-until-finished --wait-on-403 --quarantine-incomplete
pause
