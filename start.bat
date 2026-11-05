@echo off
REM Caryanams DMS - double-click to set up and start.
title Caryanams DMS
cd /d "%~dp0"
set CARYANAMS_LAUNCHER=1

REM Prefer a Python version the project supports (3.12 -> 3.9), then any.
set "PYCMD="
for %%V in (3.12 3.11 3.10 3.9) do (
  if not defined PYCMD (
    py -%%V -c "pass" >nul 2>&1 && set "PYCMD=py -%%V"
  )
)
if not defined PYCMD (
  python -c "import sys; sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo.
  echo  Python is not installed.
  echo  Install Python 3.12 from https://www.python.org/downloads/release/python-3128/
  echo  and tick "Add python.exe to PATH" in the installer, then double-click start.bat again.
  echo.
  start "" "https://www.python.org/downloads/release/python-3128/"
  pause
  exit /b 1
)

%PYCMD% run.py %*
if errorlevel 1 pause
