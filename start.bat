@echo off
rem Starts the story scanner: installs deps if missing, logs in if needed, then runs the loop.
rem Stop it with Ctrl+C, or create data\STOP from another window.
setlocal
cd /d "%~dp0"
title Story Scanner

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -c "import playwright, jinja2, tzdata" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    "%PY%" -m pip install -r requirements.txt || goto :fail
    "%PY%" -m playwright install chromium || goto :fail
)

if exist "data\STOP" (
    echo Removing old data\STOP kill-switch file.
    del "data\STOP"
)

"%PY%" -m tracker check
if errorlevel 4 goto :fail
if errorlevel 3 goto :login
if errorlevel 1 goto :fail
goto :run

:login
echo.
echo No saved session. A browser window will open: log in to Instagram there (2FA is fine).
"%PY%" -m tracker login || goto :fail
"%PY%" -m tracker check || goto :fail

:run
echo.
echo Scanning. Report: %CD%\data\report.html
"%PY%" -m tracker run
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Story scanner stopped with an error (exit code %errorlevel%). See data\logs\tracker.log
pause
exit /b 1
