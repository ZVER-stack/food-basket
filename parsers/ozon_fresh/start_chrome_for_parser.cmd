@echo off
setlocal
set "FRESH_CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%FRESH_CHROME%" set "FRESH_CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%FRESH_CHROME%" (
  echo Google Chrome was not found in Program Files.
  pause
  exit /b 1
)
if not exist "%~dp0work" mkdir "%~dp0work"
start "" "%FRESH_CHROME%" --remote-debugging-port=9223 --remote-debugging-address=127.0.0.1 --user-data-dir="%~dp0work\chrome_public" --no-first-run --no-default-browser-check "https://www.ozon.ru/category/supermarket-25000/?miniapp=supermarket"
endlocal
