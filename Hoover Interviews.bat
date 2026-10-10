@echo off
setlocal EnableExtensions
title Hoover Interviews
rem ==========================================================================
rem  Hoover Interviews.bat -- double-click to fetch Sienna's finished driver
rem  interviews from ElevenLabs, build the dossier Hoover reads, and show who
rem  has and hasn't done theirs. Safe to run as often as you like.
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
%PY% hoover_interviews.py %*
echo.
pause
