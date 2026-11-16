@echo off
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python isn't installed. Get Python 3.11 or newer from https://www.python.org/downloads/
  echo During setup, tick "Add python.exe to PATH". Then run this again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo jobbot needs Python 3.11 or newer: https://www.python.org/downloads/
  pause
  exit /b 1
)
echo Starting jobbot. Keep this window open while you use it; close it to stop.
%PY% -m jobbot serve
if errorlevel 1 pause
