@echo off
setlocal
chcp 65001 >nul
set "FRESH_PY=C:\Users\maxik\PycharmProjects\PythonProject\.venv\Scripts\python.exe"
set "FRESH_SCRIPT=C:\Users\maxik\Documents\Codex\2026-10-17\ghbd\work\scrape_one_address.py"
if not exist "%FRESH_PY%" (
  echo Не найден Python проекта: %FRESH_PY%
  pause
  exit /b 1
)
if not exist "%FRESH_SCRIPT%" (
  echo Не найден скрипт: %FRESH_SCRIPT%
  pause
  exit /b 1
)
echo Один адрес: Москва, Бакунинская улица, 60/2.
echo Сбор с нуля: 7 сборщиков, пауза 1,1 секунды, общий лимит 300 секунд.
echo Полнота каталога за 5 минут пока не обеспечена; неполный результат будет отмечен.
echo Сначала откройте Chrome через 01_open_old_chrome.cmd и проверьте доступ к товару.
echo Сохранённые паузы после защиты остаются в силе. Капча требует ручного прохождения.
echo Результат: C:\Users\maxik\Documents\Codex\2026-10-17\ghbd\outputs\single_address_ДАТА_ВРЕМЯ
echo Не запускайте второй сбор одновременно. Для остановки нажмите Ctrl+C.
"%FRESH_PY%" -u "%FRESH_SCRIPT%"
set "FRESH_EXIT=%ERRORLEVEL%"
echo.
echo Процесс завершён. Код выхода: %FRESH_EXIT%.
echo Итог и полнота указаны в summary.json. Частичные товары: partial_observations.csv.
echo Полный экспорт завершённых адресов: ozon_fresh.csv. Он может быть пустым при неполном сборе.
pause
exit /b %FRESH_EXIT%
