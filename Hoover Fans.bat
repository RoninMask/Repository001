@echo off
setlocal EnableExtensions
title Hoover Fans
rem ==========================================================================
rem  Hoover Fans.bat -- double-click to fetch Sienna's finished fan interviews
rem  (friends and family) from ElevenLabs, keep only the people who said yes
rem  to air, and add their lines to hoover_lore.json. Safe to run as often as
rem  you like. Run it again after any lore rebuild.
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
%PY% hoover_fans.py %*
echo.
pause
