@echo off
setlocal EnableExtensions
title Hoover Dashboard
rem ==========================================================================
rem  Hoover Dashboard.bat -- double-click to open the Hoover dashboard in your
rem  browser. Keep this file in the Hoover folder, next to hoover_dash.py and
rem  the tool. Leave this window open while you use the dashboard; closing it
rem  stops the dashboard (a running race is stopped and finalised first).
rem  To put it on the desktop, run "Make dashboard shortcut.bat" once.
rem ==========================================================================
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Python was not found. Install Python 3 from python.org, tick
  echo "Add python.exe to PATH" during the install, then try again.
  pause
  exit /b 1
)
if not exist "hoover_dash.py" (
  echo Cannot find hoover_dash.py in:
  echo   %CD%
  echo Run git pull in the Hoover folder to get the dashboard.
  pause
  exit /b 1
)
%PY% hoover_dash.py %*
if errorlevel 1 pause
endlocal
