@echo off
cd /d "%~dp0"
curl -s http://127.0.0.1:9222/json/version >nul 2>&1
if not errorlevel 1 goto run
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
echo Starting Chrome...
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\udemy_chrome"
timeout /t 6 /nobreak >nul
:run
git checkout -- seen.json state.json tracked.json >nul 2>&1
git pull --rebase origin main
python local_check.py --cdp
pause
