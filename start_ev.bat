@echo off
rem E.V. launcher for Windows: double-click to start. First run sets everything up.
setlocal
cd /d "%~dp0"
title E.V. Command Center

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython

if not exist ".venv\Scripts\python.exe" (
  echo Setting up E.V. for the first time - this takes a minute...
  %PY% -m venv .venv || goto :nopython
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :piperr

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env - you can add API keys in the app under Settings.
)

".venv\Scripts\python.exe" run.py --open
echo.
echo E.V. has stopped.
pause
exit /b 0

:nopython
echo.
echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during setup), then run this file again.
pause
exit /b 1

:piperr
echo.
echo Installing dependencies failed. Check your internet connection and try again.
pause
exit /b 1
