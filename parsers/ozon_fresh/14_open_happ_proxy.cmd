@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%FRESH_PY%" exit /b 1
"%FRESH_PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',9222))==0 else 1)"
if not errorlevel 1 (
  echo Прежний Chrome парсера уже открыт на порту 9222. Закройте только его окно и повторите шаг 14.
  echo Параметр прокси применяется при новом запуске Chrome.
  pause
  exit /b 1
)
"%FRESH_PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',10809))==0 else 1)"
if errorlevel 1 (
  echo HTTP-прокси Happ 127.0.0.1:10809 недоступен. Сначала запустите подключение в Happ.
  pause
  exit /b 1
)
set "FRESH_CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%FRESH_CHROME%" set "FRESH_CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%FRESH_CHROME%" set "FRESH_CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not exist "%FRESH_CHROME%" exit /b 1
echo Открываю прежний профиль C:\ozon_chrome через локальный HTTP-прокси Happ 127.0.0.1:10809.
echo Выбранный сервер и правила маршрутизации задаются в Happ.
echo Это проверка соединения: сборщик не запускается, доступ Ozon не гарантирован.
start "" "%FRESH_CHROME%" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="C:\ozon_chrome" --no-first-run --no-default-browser-check --proxy-server="http://127.0.0.1:10809" --proxy-bypass-list="localhost;127.0.0.1;[::1]" "https://www.ozon.ru/product/moloko-parmalat-protein-s-kollagenom-1-l-2952307571/"
echo Если появится капча, пройдите её вручную. Для чтения видимых цен используйте 02_check_connection.cmd.
echo Чтобы вернуться к обычному запуску: закройте это окно Chrome и используйте 01_open_old_chrome.cmd.
pause
