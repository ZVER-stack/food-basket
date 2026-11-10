@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%FRESH_PY%" exit /b 1
echo Выборочное обновление после завершения первичного обхода всех адресов.
echo Лимит 7200 секунд включает паузы. Непроверенные данные отмечаются отдельно.
echo Отсутствие товара в выборке не означает его удаления.
echo При капче пройдите её вручную в прежнем Chrome и запустите06.
"%FRESH_PY%" "%~dp0ozon_refresh.py" --seconds 7200 --readers 4 --pages-per-house 4 --max-product-checks 2 --interval 3
pause
