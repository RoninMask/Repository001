@echo off
setlocal
rem ==========================================================================
rem  Make desktop shortcut.bat -- run once per computer. Puts a "Hoover"
rem  shortcut on your desktop that runs Start Hoover.bat from this folder.
rem  If you move the Hoover folder, run this again.
rem ==========================================================================
cd /d "%~dp0"
if not exist "Start Hoover.bat" (
  echo Start Hoover.bat is not in this folder. Run this from the Hoover folder.
  pause
  exit /b 1
)
set "HOOVER_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=$env:HOOVER_DIR; $desk=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk 'Hoover.lnk')); $s.TargetPath=(Join-Path $d 'Start Hoover.bat'); $s.WorkingDirectory=$d; $s.IconLocation=(Join-Path $d 'hoover.ico'); $s.Description='Start Hoover'; $s.Save(); Write-Host ('Made: ' + (Join-Path $desk 'Hoover.lnk'))"
if errorlevel 1 (
  echo Could not make the shortcut. You can do it by hand: right-click
  echo Start Hoover.bat, Show more options, Send to, Desktop ^(create shortcut^).
  pause
  exit /b 1
)
echo The Hoover shortcut is on your desktop. It points at:
echo   %~dp0Start Hoover.bat
pause
endlocal
