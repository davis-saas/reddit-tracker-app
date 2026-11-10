@echo off
rem Double-click this file to start Reddit Tracker on Windows.
cd /d "%~dp0"

if not exist "reddit_tracker.py" (
  echo Unzip the whole folder first: right-click the zip, choose Extract All..., then open the new folder, then the reddit-tracker-app folder inside it, and double-click start-windows.bat.
  pause
  exit /b 1
)

set "PY="
py -3 -c "import sys; sys.exit(sys.version_info<(3,9))" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(sys.version_info<(3,9))" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Reddit Tracker needs Python 3.9 or newer. Get it free from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then double-click this file again.
  pause
  exit /b 1
)

%PY% reddit_tracker.py %*
if errorlevel 1 pause
