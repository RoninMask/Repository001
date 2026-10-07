@echo off
setlocal EnableExtensions
title Hoover
rem ==========================================================================
rem  Start Hoover.bat -- double-click to run Hoover. Keep this file in the
rem  Hoover folder, next to the tool. To put it on the desktop, run
rem  "Make desktop shortcut.bat" once; never copy this file anywhere else.
rem ==========================================================================
cd /d "%~dp0"

rem The ONE line to change when a new version renames the tool file:
set "HOOVER_TOOL=T11_F125_Baby_Hoover_V4_05OCT26.py"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Python was not found. Install Python 3 from python.org, tick
  echo "Add python.exe to PATH" during the install, then try again.
  pause
  exit /b 1
)
if not exist "%HOOVER_TOOL%" (
  echo Cannot find %HOOVER_TOOL% in:
  echo   %CD%
  echo Start Hoover.bat has to stay in the Hoover folder, next to the tool.
  echo If the tool was renamed for a new version, update HOOVER_TOOL in this file.
  pause
  exit /b 1
)

:menu
cls
%PY% "%HOOVER_TOOL%" --version
echo.
echo   1  Audio check     beep on your speakers, check the ElevenLabs key
echo   2  Pick devices    choose the cable and your speakers
echo   3  Start live      template lines, speech on
echo   4  Start live      no speech
echo   5  Replay a capture, fast, no speech
echo   6  Start live with extra options you type
echo   7  Open the output folder
echo   Q  Quit
echo.
set "CHOICE="
set /p "CHOICE=Choose: "
if /i "%CHOICE%"=="1" goto audio
if /i "%CHOICE%"=="2" goto pick
if /i "%CHOICE%"=="3" goto live_speech
if /i "%CHOICE%"=="4" goto live_quiet
if /i "%CHOICE%"=="5" goto replay
if /i "%CHOICE%"=="6" goto live_custom
if /i "%CHOICE%"=="7" goto output
if /i "%CHOICE%"=="q" goto end
goto menu

:audio
echo.
%PY% "%HOOVER_TOOL%" --audio-check
echo.
pause
goto menu

:pick
echo.
%PY% "%HOOVER_TOOL%" --pick-devices
echo.
pause
goto menu

:live_speech
call :before_live
%PY% "%HOOVER_TOOL%" --source live --writer template --speech elevenlabs
goto after_run

:live_quiet
call :before_live
%PY% "%HOOVER_TOOL%" --source live --writer template
goto after_run

:live_custom
echo.
echo Type extra options, for example:  --writer hybrid --speech elevenlabs
set "EXTRA="
set /p "EXTRA=Options: "
call :before_live
%PY% "%HOOVER_TOOL%" --source live %EXTRA%
goto after_run

:replay
echo.
echo Drag a .bin capture onto this window, then press Enter.
set "BIN="
set /p "BIN=Capture: "
if not defined BIN goto menu
set "BIN=%BIN:"=%"
%PY% "%HOOVER_TOOL%" --source fast --replay "%BIN%"
goto after_run

:output
if not exist "hoover_v3_out" mkdir "hoover_v3_out"
start "" "hoover_v3_out"
goto menu

:before_live
echo.
echo Hoover is starting. To stop it cleanly, click this window and press Ctrl+C.
echo If Windows then asks "Terminate batch job (Y/N)?", answer N to come back here.
echo.
exit /b 0

:after_run
echo.
echo Hoover has stopped. The run is in the hoover_v3_out folder.
pause
goto menu

:end
endlocal
