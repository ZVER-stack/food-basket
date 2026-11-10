@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "FRESH_PY=%~dp0..\.venv\Scripts\python.exe"
if exist "%FRESH_PY%" (
  "%FRESH_PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',9222))==0 else 1)"
  if not errorlevel 1 (
    echo Chrome парсера уже открыт. Закройте только его окно и повторите шаг 01.
    echo Параметр без прокси применяется при новом запуске Chrome.
    pause
    exit /b 1
  )
)
set "FRESH_CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%FRESH_CHROME%" set "FRESH_CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%FRESH_CHROME%" (
  echo Не найден установленный Google Chrome.
  pause
  exit /b 1
)
if not exist "C:\ozon_chrome" (
  echo Не найден профиль C:\ozon_chrome, который использовал старый парсер.
  echo Новый профиль автоматически не создаётся.
  pause
  exit /b 1
)
echo Открываю Chrome с прежним профилем парсера C:\ozon_chrome.
echo Для этого Chrome прокси отключён параметром --no-proxy-server.
echo Дождитесь карточки товара. В этом окне нужна публичная витрина без входа в аккаунт.
echo Если появится защита, сбор не запускайте.
start "" "%FRESH_CHROME%" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="C:\ozon_chrome" --no-first-run --no-default-browser-check --no-proxy-server "https://www.ozon.ru/product/moloko-parmalat-protein-s-kollagenom-1-l-2952307571/"
echo Затем запустите 02_check_connection.cmd из этой же папки.
echo Если порт 9222 недоступен, закройте именно это окно Chrome и повторите шаг 01.
pause
