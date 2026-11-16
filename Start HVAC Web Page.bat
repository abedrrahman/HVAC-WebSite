@echo off
rem Double-click to start the HVAC web page. It runs in a minimised window: close that window to stop the web page.
cd /d "%~dp0"
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo Starting the HVAC web page...
    start "HVAC web page - close this window to stop it" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
    rem wait until the login page answers, up to 2 minutes (the database may need a moment)
    powershell -NoProfile -Command "foreach ($i in 1..40) { try { Invoke-WebRequest http://127.0.0.1:8000/login/ -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { Start-Sleep 3 } }; exit 1"
    if errorlevel 1 (
        echo The web page did not start. Open the minimised window to see the error.
        pause
        exit /b 1
    )
) else (
    echo The HVAC web page is already running.
)
start "" http://localhost:8000
rem Open the address for the phone app too. It is new on every start: type it into the app's Settings.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1"
echo.
echo The address is also saved in PHONE_ADDRESS.txt. It can take a minute before it answers.
pause
