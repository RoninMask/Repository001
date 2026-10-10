@echo off
setlocal EnableExtensions
title Hoover Lore
rem ==========================================================================
rem  Hoover Lore.bat -- double-click to rebuild the F1 history and track lore
rem  cards from the research files in ingest\lore\raw. Safe to
rem  run as often as you like; it prints what it held back.
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
%PY% hoover_lore.py %*
echo.
pause
