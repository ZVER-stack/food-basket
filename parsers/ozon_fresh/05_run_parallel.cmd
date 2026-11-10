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
echo Семь общих сборщиков категорий и карточек в уже открытом Chrome.
echo Общий справочник SKU: карточки только для новых и изменившихся предложений.
echo Перенос обычной цены отмечается как непроверенный для текущего адреса.
echo Адрес меняется только после завершения всех запросов предыдущего дома.
echo Этот режим ограничен двумя часами. Для обхода до конца используйте08.
echo Интервал: сначала 5 секунд; после HTTP403/429 +1 секунда, максимум10.
echo Достигнутая ступень сохраняется при перезапуске. Ожидание доступа и Retry-After соблюдаются.
"%FRESH_PY%" "%~dp0run_parser.py" --action full --price-workers 7 --catalog-workers 7 --interval 5 --max-interval 10 --http-error-steps 6,7,8,9,10 --http-error-statuses 403,429 --price-policy catalog_metadata_cache --wait-on-403 --quarantine-incomplete
pause
