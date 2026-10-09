@echo off
setlocal
rem ==========================================================================
rem  Make dashboard shortcut.bat -- run once per computer. Puts a "Hoover
rem  Dashboard" shortcut on your desktop that runs Hoover Dashboard.bat from
rem  this folder. If you move the Hoover folder, run this again.
rem ==========================================================================
cd /d "%~dp0"
if not exist "Hoover Dashboard.bat" (
  echo Hoover Dashboard.bat is not in this folder. Run this from the Hoover folder.
  pause
  exit /b 1
)
set "HOOVER_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=$env:HOOVER_DIR; $desk=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk 'Hoover Dashboard.lnk')); $s.TargetPath=(Join-Path $d 'Hoover Dashboard.bat'); $s.WorkingDirectory=$d; $s.IconLocation=(Join-Path $d 'hoover.ico'); $s.Description='Open the Hoover dashboard'; $s.Save(); Write-Host ('Made: ' + (Join-Path $desk 'Hoover Dashboard.lnk'))"
if errorlevel 1 (
  echo Could not make the shortcut. You can do it by hand: right-click
  echo Hoover Dashboard.bat, Show more options, Send to, Desktop ^(create shortcut^).
  pause
  exit /b 1
)
echo The Hoover Dashboard shortcut is on your desktop. It points at:
echo   %~dp0Hoover Dashboard.bat
pause
endlocal
