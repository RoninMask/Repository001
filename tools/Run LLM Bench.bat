@echo off
setlocal EnableExtensions
title Hoover LLM Bench
rem  Run LLM Bench.bat -- double-click. Lives in tools\, runs from the repo folder.
cd /d "%~dp0.."
set "BENCH=tools\T13_LLM_Bench_V1_26OCT26.py"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Python was not found.
  pause
  exit /b 1
)
:menu
cls
echo T13 LLM Bench -- local llama3.2 vs cloud Haiku, same prompts
echo.
echo   1  Dry run        build the prompts, show sizes, send nothing
echo   2  Quick check    5 beats, both lanes (about 1 minute)
echo   3  Full bench     40 beats, both lanes (about 6 to 8 minutes)
echo                     For the real test: F1 25 on track at 60 fps, OBS recording.
echo   4  Open results folder
echo   Q  Quit
echo.
set "CHOICE="
set /p "CHOICE=Choose: "
if /i "%CHOICE%"=="1" ( %PY% "%BENCH%" --dry-run & pause & goto menu )
if /i "%CHOICE%"=="2" ( %PY% "%BENCH%" --n 5 & pause & goto menu )
if /i "%CHOICE%"=="3" ( %PY% "%BENCH%" --n 40 & pause & goto menu )
if /i "%CHOICE%"=="4" ( if not exist hoover_bench_out mkdir hoover_bench_out & start "" hoover_bench_out & goto menu )
if /i "%CHOICE%"=="q" goto end
goto menu
:end
endlocal
