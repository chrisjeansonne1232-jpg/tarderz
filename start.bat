@echo off
rem One command to run everything on Windows (double-click, or: start.bat --ipad)
rem Paper trading only: nothing here can place an order.
setlocal
cd /d "%~dp0"

set "HOST="
if /I "%~1"=="--ipad" set "HOST=0.0.0.0"

rem --- 1. Python 3.11+ --------------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto havepy
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto havepy
echo Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then run start.bat again.
pause
exit /b 1
:havepy

rem --- 2. Private environment + dependencies -----------------------------------
if not exist .venv\Scripts\python.exe (
  echo Setting up a private Python environment in .venv
  %PY% -m venv .venv || goto fail
)
echo Installing/checking dependencies...
.venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto fail

rem --- 3. One-shot check of the live markets ------------------------------------
if not exist data mkdir data
echo Checking the live Polymarket markets (full output: data\discover.txt)
.venv\Scripts\python -m polybot discover > data\discover.txt 2>&1
findstr /R /C:"^=== " /C:"rules verified" /C:"fee model used" /C:"NOT FOUND" /C:"failed" data\discover.txt

rem --- 4. Start; open the browser a few seconds later ----------------------------
for /f %%p in ('.venv\Scripts\python -c "from polybot.config import load_config; print(load_config('config.toml').dashboard.dashboard_port)"') do set "PORT=%%p"
start "" /min cmd /c "timeout /t 6 /nobreak >nul & start http://127.0.0.1:%PORT%"
echo Starting paper trading - dashboard at http://127.0.0.1:%PORT% - press Ctrl+C to stop
echo (Windows may put the PC to sleep; set Power ^& sleep to "Never" while this runs.)
if defined HOST (
  .venv\Scripts\python -m polybot run --dashboard --host %HOST%
) else (
  .venv\Scripts\python -m polybot run --dashboard
)
goto end

:fail
echo Setup failed - see the messages above.
pause
exit /b 1
:end
endlocal
